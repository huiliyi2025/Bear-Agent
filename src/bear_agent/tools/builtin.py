"""Built-in filesystem and shell tools.

Security model: ``permission`` levels drive the policy layer:
- read_only  -> always allowed
- modify     -> blocked in plan mode
- dangerous  -> dangerous-pattern commands trigger interactive confirmation
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from bear_agent.tools.base import Tool, ToolResult

MAX_OUTPUT_CHARS = 12_000
MAX_GREP_HITS = 200
MAX_GLOB_HITS = 500

READ_FILE_SCHEMA = {
    "type": "object",
    "description": "Read a UTF-8 text file (optionally a slice of lines).",
    "properties": {
        "path": {
            "type": "string",
            "description": "Absolute path, or path relative to the working directory.",
        },
        "offset": {"type": "integer", "description": "0-based starting line index."},
        "limit": {"type": "integer", "description": "Maximum number of lines to read."},
    },
    "required": ["path"],
}

WRITE_FILE_SCHEMA = {
    "type": "object",
    "description": "Write (overwrite) a UTF-8 text file, creating parent directories.",
    "properties": {
        "path": {
            "type": "string",
            "description": "Absolute path, or path relative to the working directory.",
        },
        "content": {"type": "string", "description": "Full text content to write."},
    },
    "required": ["path", "content"],
}

EDIT_FILE_SCHEMA = {
    "type": "object",
    "description": "Make exactly one exact string replacement in a file. Fails if old_string is not found or is ambiguous.",
    "properties": {
        "path": {"type": "string"},
        "old_string": {
            "type": "string",
            "description": "Exact text to locate (must occur exactly once).",
        },
        "new_string": {"type": "string", "description": "Replacement text."},
    },
    "required": ["path", "old_string", "new_string"],
}

SEARCH_FILES_SCHEMA = {
    "type": "object",
    "description": "List files matching a glob pattern inside a directory.",
    "properties": {
        "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'."},
        "root": {
            "type": "string",
            "description": "Directory to search from (defaults to working directory).",
        },
    },
    "required": ["pattern"],
}

GREP_FILES_SCHEMA = {
    "type": "object",
    "description": "Regex-search file contents, returning matches as 'path:line: text'.",
    "properties": {
        "pattern": {"type": "string", "description": "Python regular expression."},
        "glob": {"type": "string", "description": "File-name filter, e.g. '*.py'."},
        "root": {
            "type": "string",
            "description": "Directory to search from (defaults to working directory).",
        },
    },
    "required": ["pattern"],
}

RUN_SHELL_SCHEMA = {
    "type": "object",
    "description": "Run a shell command in the working directory and capture stdout/stderr.",
    "properties": {
        "command": {"type": "string"},
        "timeout": {
            "type": "number",
            "description": "Timeout in seconds (default 60).",
        },
    },
    "required": ["command"],
}


def _resolve(base: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _is_text_file(path: Path, sample_size: int = 8192) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(sample_size)
    except OSError:
        return False
    return b"\x00" not in head


def _read_file(base: Path, args: dict[str, Any]) -> ToolResult:
    path = _resolve(base, str(args.get("path") or "."))
    if not path.exists():
        return ToolResult(ok=False, error=f"file not found: {path}")
    if path.is_dir():
        return ToolResult(ok=False, error=f"path is a directory: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult(ok=False, error="not a UTF-8 text file (binary?)")
    except OSError as exc:
        return ToolResult(ok=False, error=str(exc))

    lines = text.splitlines()
    offset = int(args.get("offset") or 0)
    if offset < 0 or offset > len(lines):
        return ToolResult(
            ok=False, error=f"offset {offset} out of range (0-{len(lines)})"
        )
    limit = args.get("limit")
    if limit is None:
        selected = lines[offset:]
    else:
        limit = int(limit)
        if limit < 0:
            return ToolResult(ok=False, error="limit must be >= 0")
        selected = lines[offset : offset + limit]

    body = "\n".join(selected)
    header = f"[file {path} | {len(lines)} lines | showing {len(selected)}]"
    if body:
        body += "\n"
    return ToolResult(ok=True, content=f"{header}\n{body}")


def _write_file(base: Path, args: dict[str, Any]) -> ToolResult:
    path = _resolve(base, str(args.get("path") or ""))
    content = str(args.get("content") or "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(ok=True, content=f"wrote {path} ({len(content)} chars)")


def _edit_file(base: Path, args: dict[str, Any]) -> ToolResult:
    path = _resolve(base, str(args.get("path") or ""))
    old_string = str(args.get("old_string") or "")
    new_string = str(args.get("new_string") or "")
    if not old_string:
        return ToolResult(ok=False, error="old_string must not be empty")
    if not path.exists():
        return ToolResult(ok=False, error=f"file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult(ok=False, error=str(exc))
    count = text.count(old_string)
    if count == 0:
        return ToolResult(ok=False, error="old_string not found in file")
    if count > 1:
        return ToolResult(
            ok=False,
            error=f"old_string is ambiguous ({count} occurrences); provide more context",
        )
    try:
        path.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
    except OSError as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(ok=True, content=f"edited {path} (1 replacement)")


def _search_files(base: Path, args: dict[str, Any]) -> ToolResult:
    pattern = str(args.get("pattern") or "**/*")
    root = _resolve(base, str(args.get("root") or "."))
    if not root.exists():
        return ToolResult(ok=False, error=f"root not found: {root}")
    if root.is_file():
        return ToolResult(ok=False, error=f"root is a file, not a directory: {root}")
    matches = sorted(p for p in root.glob(pattern) if p.is_file())
    if not matches:
        return ToolResult(ok=True, content=f"no files matched {pattern!r} in {root}")
    lines = [str(p.relative_to(root)) for p in matches[:MAX_GLOB_HITS]]
    body = "\n".join(lines)
    if len(matches) > MAX_GLOB_HITS:
        body += f"\n... {len(matches) - MAX_GLOB_HITS} more omitted"
    return ToolResult(ok=True, content=f"{len(matches)} files:\n{body}")


def _grep_files(base: Path, args: dict[str, Any]) -> ToolResult:
    pattern = str(args.get("pattern") or "")
    file_glob = str(args.get("glob") or "*")
    root = _resolve(base, str(args.get("root") or "."))
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolResult(ok=False, error=f"invalid regex: {exc}")
    if not root.exists():
        return ToolResult(ok=False, error=f"root not found: {root}")

    files = (
        [root] if root.is_file() else [p for p in root.rglob(file_glob) if p.is_file()]
    )
    hits: list[tuple[Path, int, str]] = []
    for path in files:
        if not _is_text_file(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            if regex.search(line):
                hits.append((path, lineno, line))
                if len(hits) >= MAX_GREP_HITS:
                    break
        if len(hits) >= MAX_GREP_HITS:
            break
    if not hits:
        return ToolResult(ok=True, content=f"no matches for {pattern!r}")
    ref = root if root.is_dir() else root.parent
    lines = [f"{p.relative_to(ref)}:{n}: {text}" for p, n, text in hits]
    if len(lines) >= MAX_GREP_HITS:
        lines.append(f"... truncated at {MAX_GREP_HITS} matches")
    return ToolResult(ok=True, content="\n".join(lines))


def _run_shell(base: Path, args: dict[str, Any]) -> ToolResult:
    command = str(args.get("command") or "")
    timeout = float(args.get("timeout") or 60)
    if not command.strip():
        return ToolResult(ok=False, error="empty command")
    try:
        proc = subprocess.run(
            command,
            check=False,
            shell=True,
            cwd=str(base),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, error=f"command timed out after {timeout}s")
    except OSError as exc:
        return ToolResult(ok=False, error=str(exc))

    out = proc.stdout
    if proc.stderr.strip():
        out += "\n[stderr]\n" + proc.stderr
    out = (out or "(no output)").strip()
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
    summary = f"$ {command}\n{out}\n[exit code {proc.returncode}]"
    if proc.returncode == 0:
        return ToolResult(ok=True, content=summary)
    return ToolResult(ok=False, error=summary)


def builtin_tools(base: str | Path) -> list[Tool]:
    """Create all built-in tools bound to a working directory."""
    resolved = Path(base).resolve()

    def read_file(args: dict[str, Any]) -> ToolResult:
        return _read_file(resolved, args)

    def write_file(args: dict[str, Any]) -> ToolResult:
        return _write_file(resolved, args)

    def edit_file(args: dict[str, Any]) -> ToolResult:
        return _edit_file(resolved, args)

    def search_files(args: dict[str, Any]) -> ToolResult:
        return _search_files(resolved, args)

    def grep_files(args: dict[str, Any]) -> ToolResult:
        return _grep_files(resolved, args)

    def run_shell(args: dict[str, Any]) -> ToolResult:
        return _run_shell(resolved, args)

    return [
        Tool(
            "read_file",
            "Read a UTF-8 text file, optionally a slice of lines.",
            READ_FILE_SCHEMA,
            read_file,
            "read_only",
        ),
        Tool(
            "write_file",
            "Write (overwrite) a UTF-8 text file, creating parent directories.",
            WRITE_FILE_SCHEMA,
            write_file,
            "modify",
        ),
        Tool(
            "edit_file",
            "Make a single exact string replacement in a file.",
            EDIT_FILE_SCHEMA,
            edit_file,
            "modify",
        ),
        Tool(
            "search_files",
            "List files matching a glob pattern inside a directory.",
            SEARCH_FILES_SCHEMA,
            search_files,
            "read_only",
        ),
        Tool(
            "grep_files",
            "Regex-search file contents, returning 'path:line: text'.",
            GREP_FILES_SCHEMA,
            grep_files,
            "read_only",
        ),
        Tool(
            "run_shell",
            "Run a shell command in the working directory and capture output.",
            RUN_SHELL_SCHEMA,
            run_shell,
            "dangerous",
        ),
    ]
