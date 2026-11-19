"""File discovery and content search tools.

Search is workspace-scoped and skips VCS/dependency/cache directories by
default so the model does not drown in generated files.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from bear_agent.policy.path_guard import PathAccessError
from bear_agent.tools.base import Tool, ToolContext, ToolResult

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".bear",
}

MAX_GLOB_RESULTS = 500
MAX_GREP_RESULTS = 200
MAX_GREP_FILE_BYTES = 2 * 1024 * 1024


class GlobArgs(BaseModel):
    pattern: str = Field(
        description="Glob pattern relative to root, e.g. '**/*.py' or 'src/**/*.ts'."
    )
    root: str = Field(default=".", description="Directory to search from.")


class GrepArgs(BaseModel):
    pattern: str = Field(description="Python regular expression to search for.")
    glob: str = Field(default="**/*", description="File glob filter, e.g. '**/*.py'.")
    root: str = Field(default=".", description="Directory to search from.")
    case_sensitive: bool = Field(default=True)
    max_results: int = Field(default=100, ge=1, le=MAX_GREP_RESULTS)


def _ignored(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part in IGNORED_DIRS for part in relative.parts)


def _error(message: str, kind: str) -> ToolResult:
    return ToolResult(content=message, is_error=True, error_kind=kind)


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files by glob pattern inside the workspace. Skips .git, virtualenvs, "
        "node_modules and build/cache directories."
    )
    args_model = GlobArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GlobArgs)
        pattern_path = Path(args.pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            return _error(
                "pattern must be relative to root and must not contain '..'", "invalid_argument"
            )
        try:
            root = ctx.path_guard.resolve(args.root, mode="read")
        except PathAccessError as exc:
            return _error(str(exc), "path_denied")
        if not root.exists():
            return _error(f"root not found: {root}", "not_found")
        if not root.is_dir():
            return _error(f"root is not a directory: {root}", "invalid_argument")
        matches: list[str] = []
        try:
            for candidate in root.glob(args.pattern):
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                if _ignored(candidate, root):
                    continue
                try:
                    ctx.path_guard.resolve(str(candidate), mode="read")
                except PathAccessError:
                    continue
                matches.append(str(candidate.relative_to(root)).replace("\\", "/"))
                if len(matches) >= MAX_GLOB_RESULTS:
                    break
        except (OSError, ValueError) as exc:
            return _error(f"glob failed: {exc}", "invalid_argument")
        matches.sort()
        if not matches:
            return ToolResult(content=f"no files matched {args.pattern!r} in {root}")
        body = "\n".join(matches)
        if len(matches) >= MAX_GLOB_RESULTS:
            body += f"\n... truncated at {MAX_GLOB_RESULTS} results"
        return ToolResult(
            content=f"{len(matches)} file(s):\n{body}",
            meta={"root": str(root), "count": len(matches)},
        )


class GrepTool(Tool):
    name = "grep"
    description = (
        "Regex-search file contents and return 'path:line: text'. Use glob to "
        "narrow file types. Skips binary and ignored directories."
    )
    args_model = GrepArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GrepArgs)
        glob_path = Path(args.glob)
        if glob_path.is_absolute() or ".." in glob_path.parts:
            return _error(
                "glob must be relative to root and must not contain '..'", "invalid_argument"
            )
        flags = 0 if args.case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(args.pattern, flags)
        except re.error as exc:
            return _error(f"invalid regex: {exc}", "invalid_argument")
        try:
            root = ctx.path_guard.resolve(args.root, mode="read")
        except PathAccessError as exc:
            return _error(str(exc), "path_denied")
        if not root.exists():
            return _error(f"root not found: {root}", "not_found")
        if not root.is_dir():
            return _error(f"root is not a directory: {root}", "invalid_argument")

        hits: list[str] = []
        scanned = 0
        try:
            for candidate in root.glob(args.glob):
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                if _ignored(candidate, root):
                    continue
                try:
                    if candidate.stat().st_size > MAX_GREP_FILE_BYTES:
                        continue
                    with candidate.open("rb") as handle:
                        sample = handle.read(8192)
                    if b"\x00" in sample:
                        continue
                    lines = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    continue
                scanned += 1
                relative = str(candidate.relative_to(root)).replace("\\", "/")
                for lineno, line in enumerate(lines, 1):
                    if regex.search(line):
                        text = line.strip()
                        if len(text) > 400:
                            text = text[:400] + "…"
                        hits.append(f"{relative}:{lineno}: {text}")
                        if len(hits) >= args.max_results:
                            break
                if len(hits) >= args.max_results:
                    break
        except (OSError, ValueError) as exc:
            return _error(f"grep failed: {exc}", "invalid_argument")

        if not hits:
            return ToolResult(
                content=f"no matches for {args.pattern!r} (files scanned: {scanned})",
                meta={"scanned": scanned, "matches": 0},
            )
        body = "\n".join(hits)
        if len(hits) >= args.max_results:
            body += f"\n... truncated at {args.max_results} matches"
        return ToolResult(
            content=body,
            meta={"scanned": scanned, "matches": len(hits)},
        )
