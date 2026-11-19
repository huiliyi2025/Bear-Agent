"""Filesystem tools with workspace containment.

Every path goes through ``PathGuard`` first. Writes are atomic (temp file in
the same directory + replace) so a crash cannot leave a half-written source
file.
"""

from __future__ import annotations

import contextlib
import difflib
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from bear_agent.core.errors import ClaimError
from bear_agent.core.modes import ToolSideEffect
from bear_agent.policy.path_guard import PathAccessError
from bear_agent.tools.base import ClaimRequest, Tool, ToolContext, ToolResult

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_READ_LINES = 5_000


class ReadFileArgs(BaseModel):
    path: str = Field(description="Absolute path or path relative to the workspace.")
    offset: int = Field(default=0, ge=0, description="0-based first line index.")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines to return.")


class WriteFileArgs(BaseModel):
    path: str = Field(description="Absolute path or path relative to the workspace.")
    content: str = Field(description="Full UTF-8 content to write.")


class EditFileArgs(BaseModel):
    path: str = Field(description="Absolute path or path relative to the workspace.")
    old_string: str = Field(description="Exact text to replace; must occur exactly once.")
    new_string: str = Field(description="Replacement text.")


class ListDirArgs(BaseModel):
    path: str = Field(default=".", description="Directory to list.")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def _is_binary(path: Path, sample_size: int = 8192) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(sample_size)
    except OSError:
        return False


def _error(message: str, kind: str) -> ToolResult:
    return ToolResult(content=message, is_error=True, error_kind=kind)


def _resolve_read(ctx: ToolContext, raw: str) -> Path:
    return ctx.path_guard.resolve(raw, mode="read")


def _resolve_write(ctx: ToolContext, raw: str) -> Path:
    return ctx.path_guard.resolve(raw, mode="write")


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file, optionally a slice of lines. Returns raw file "
        "text with a line-range header. Use offset/limit for very large files."
    )
    args_model = ReadFileArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ReadFileArgs)
        try:
            path = _resolve_read(ctx, args.path)
        except PathAccessError as exc:
            return _error(str(exc), "path_denied")
        if not path.exists():
            return _error(f"file not found: {path}", "not_found")
        if path.is_dir():
            return _error(f"path is a directory: {path}", "invalid_argument")
        if not path.is_file():
            return _error(f"path is not a regular file: {path}", "invalid_argument")
        try:
            size = path.stat().st_size
            if size > MAX_READ_BYTES:
                return _error(
                    f"file is too large ({size} bytes; limit {MAX_READ_BYTES}); "
                    "use offset/limit or grep",
                    "too_large",
                )
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _error("file is not valid UTF-8 text", "binary")
        except OSError as exc:
            return _error(str(exc), "io_error")

        lines = text.splitlines()
        start = args.offset
        if start > len(lines):
            return _error(
                f"offset {start} is past end of file ({len(lines)} lines)",
                "invalid_argument",
            )
        end = len(lines) if args.limit is None else min(len(lines), start + args.limit)
        selected = lines[start:end]
        body = "\n".join(selected)
        header = (
            f"[file {ctx.path_guard.relative(path)} | {len(lines)} lines | "
            f"showing lines {start + 1}-{end}]"
        )
        if end < len(lines):
            header += f" [truncated; call with offset={end} for more]"
        return ToolResult(
            content=f"{header}\n{body}" + ("\n" if body else ""),
            files_changed=[],
            meta={"path": str(path), "lines": len(lines), "shown": len(selected)},
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write (overwrite) a UTF-8 text file. Creates parent directories. "
        "Prefer edit_file for small changes to existing files."
    )
    args_model = WriteFileArgs
    side_effect = ToolSideEffect.WRITE
    idempotent = True
    concurrency_safe = False

    def claim_paths(self, args: BaseModel) -> list[ClaimRequest]:
        assert isinstance(args, WriteFileArgs)
        return [ClaimRequest(path=args.path, scope="file")]

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, WriteFileArgs)
        if not ctx.approved:
            return _error(
                "write_file invoked outside the tool executor authorization chain",
                "approval_required",
            )
        try:
            path = _resolve_write(ctx, args.path)
        except PathAccessError as exc:
            return _error(str(exc), "path_denied")
        existed = path.exists()
        try:
            if ctx.assert_write_allowed is not None:
                await ctx.assert_write_allowed(str(path))
        except ClaimError as exc:
            return _error(f"write fenced: {exc}", "claim_fenced")
        try:
            _atomic_write(path, args.content)
        except OSError as exc:
            return _error(f"write failed: {exc}", "io_error")
        rel = ctx.path_guard.relative(path)
        action = "overwrote" if existed else "created"
        return ToolResult(
            content=f"{action} {rel} ({len(args.content)} chars)",
            files_changed=[rel],
            meta={"path": str(path), "existed": existed, "chars": len(args.content)},
        )


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace exactly one occurrence of old_string with new_string in a file. "
        "Fails when old_string is absent or ambiguous; include more context."
    )
    args_model = EditFileArgs
    side_effect = ToolSideEffect.WRITE
    idempotent = True
    concurrency_safe = False

    def claim_paths(self, args: BaseModel) -> list[ClaimRequest]:
        assert isinstance(args, EditFileArgs)
        return [ClaimRequest(path=args.path, scope="file")]

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, EditFileArgs)
        if not ctx.approved:
            return _error(
                "edit_file invoked outside the tool executor authorization chain",
                "approval_required",
            )
        if not args.old_string:
            return _error("old_string must not be empty", "invalid_argument")
        if args.old_string == args.new_string:
            return _error("old_string and new_string are identical", "invalid_argument")
        try:
            path = _resolve_write(ctx, args.path)
        except PathAccessError as exc:
            return _error(str(exc), "path_denied")
        if not path.exists():
            return _error(f"file not found: {path}", "not_found")
        if path.is_dir():
            return _error(f"path is a directory: {path}", "invalid_argument")
        if not path.is_file():
            return _error(f"path is not a regular file: {path}", "invalid_argument")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            return _error(f"cannot read file: {exc}", "io_error")
        count = text.count(args.old_string)
        if count == 0:
            return _error(
                "old_string not found. Re-read the file and copy the exact text.",
                "not_found",
            )
        if count > 1:
            return _error(
                f"old_string occurs {count} times; include more surrounding context",
                "ambiguous_match",
            )
        updated = text.replace(args.old_string, args.new_string, 1)
        try:
            if ctx.assert_write_allowed is not None:
                await ctx.assert_write_allowed(str(path))
        except ClaimError as exc:
            return _error(f"write fenced: {exc}", "claim_fenced")
        try:
            _atomic_write(path, updated)
        except OSError as exc:
            return _error(f"write failed: {exc}", "io_error")
        rel = ctx.path_guard.relative(path)
        diff = "\n".join(
            list(
                difflib.unified_diff(
                    text.splitlines(),
                    updated.splitlines(),
                    fromfile=rel,
                    tofile=rel,
                    lineterm="",
                )
            )[:120]
        )
        return ToolResult(
            content=f"edited {rel} (1 replacement)\n{diff}",
            display=diff,
            files_changed=[rel],
            meta={"path": str(path), "replacements": 1},
        )


class ListDirTool(Tool):
    name = "list_dir"
    description = (
        "List entries in a directory with type and size. Directories are marked "
        "with a trailing slash."
    )
    args_model = ListDirArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ListDirArgs)
        try:
            path = _resolve_read(ctx, args.path)
        except PathAccessError as exc:
            return _error(str(exc), "path_denied")
        if not path.exists():
            return _error(f"directory not found: {path}", "not_found")
        if not path.is_dir():
            return _error(f"not a directory: {path}", "invalid_argument")
        try:
            entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name))
        except OSError as exc:
            return _error(str(exc), "io_error")
        lines: list[str] = []
        for entry in entries[:1000]:
            try:
                if entry.is_dir():
                    lines.append(f"{entry.name}/")
                else:
                    lines.append(f"{entry.name}\t{entry.stat().st_size} bytes")
            except OSError:
                lines.append(f"{entry.name}\t<unreadable>")
        if len(entries) > 1000:
            lines.append(f"... {len(entries) - 1000} more entries omitted")
        return ToolResult(
            content=f"[dir {ctx.path_guard.relative(path)} | {len(entries)} entries]\n"
            + "\n".join(lines),
            meta={"path": str(path), "entries": len(entries)},
        )
