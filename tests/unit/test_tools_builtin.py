from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from bear_agent.core.cancel import CancelToken
from bear_agent.core.errors import ClaimError
from bear_agent.policy.path_guard import PathGuard
from bear_agent.tools.base import Tool, ToolContext, ToolResult
from bear_agent.tools.builtin import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)


async def _call(
    tool: Tool,
    workspace: Path,
    *,
    assert_write_allowed: object | None = None,
    approved: bool = True,
    **kwargs: object,
) -> ToolResult:
    args = tool.args_model.model_validate(kwargs)
    ctx = ToolContext(
        cwd=workspace,
        session_id="test",
        cancel=CancelToken(),
        path_guard=PathGuard(workspace),
        approved=approved,
        assert_write_allowed=assert_write_allowed,  # type: ignore[arg-type]
    )
    return await tool.run(args, ctx)


@pytest.mark.asyncio
async def test_read_file_and_slice(workspace: Path) -> None:
    result = await _call(ReadFileTool(), workspace, path="src/app.py")
    assert not result.is_error
    assert "def add" in result.content
    sliced = await _call(ReadFileTool(), workspace, path="src/app.py", offset=1, limit=1)
    assert "return a + b" in sliced.content
    assert "showing lines 2-2" in sliced.content


@pytest.mark.asyncio
async def test_read_missing_and_sensitive(workspace: Path) -> None:
    missing = await _call(ReadFileTool(), workspace, path="nope.txt")
    assert missing.is_error and missing.error_kind == "not_found"
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    denied = await _call(ReadFileTool(), workspace, path=".env")
    assert denied.is_error and denied.error_kind == "path_denied"


@pytest.mark.asyncio
async def test_write_and_edit(workspace: Path) -> None:
    written = await _call(WriteFileTool(), workspace, path="new/dir/file.txt", content="one two\n")
    assert not written.is_error
    target = workspace / "new" / "dir" / "file.txt"
    assert target.read_text(encoding="utf-8") == "one two\n"
    edited = await _call(
        EditFileTool(),
        workspace,
        path="new/dir/file.txt",
        old_string="two",
        new_string="three",
    )
    assert not edited.is_error
    assert target.read_text(encoding="utf-8") == "one three\n"
    ambiguous = await _call(
        EditFileTool(),
        workspace,
        path="new/dir/file.txt",
        old_string="e",
        new_string="E",
    )
    assert ambiguous.is_error and ambiguous.error_kind == "ambiguous_match"


@pytest.mark.asyncio
async def test_list_glob_and_grep(workspace: Path) -> None:
    listed = await _call(ListDirTool(), workspace, path=".")
    assert "hello.txt" in listed.content
    assert "src/" in listed.content

    globbed = await _call(GlobTool(), workspace, pattern="**/*.py")
    assert "src/app.py" in globbed.content

    grepped = await _call(GrepTool(), workspace, pattern="return a \\+ b", glob="**/*.py")
    assert "src/app.py:2" in grepped.content

    no_match = await _call(GrepTool(), workspace, pattern="zzz-not-there")
    assert "no matches" in no_match.content


@pytest.mark.asyncio
async def test_search_rejects_escape(workspace: Path) -> None:
    result = await _call(GlobTool(), workspace, pattern="../*.txt")
    assert result.is_error
    grep = await _call(GrepTool(), workspace, pattern="x", root="../")
    assert grep.is_error
    grep_glob = await _call(GrepTool(), workspace, pattern="x", glob="../*")
    assert grep_glob.is_error


@pytest.mark.asyncio
async def test_write_is_fenced_at_write_site(workspace: Path) -> None:
    async def deny(_path: str) -> None:
        raise ClaimError("claim lost before write")

    result = await _call(
        WriteFileTool(),
        workspace,
        assert_write_allowed=deny,
        path="fenced.txt",
        content="should not land",
    )
    assert result.is_error and result.error_kind == "claim_fenced"
    assert not (workspace / "fenced.txt").exists()


@pytest.mark.asyncio
async def test_edit_is_fenced_at_write_site(workspace: Path) -> None:
    async def deny(_path: str) -> None:
        raise ClaimError("claim lost before write")

    target = workspace / "fenced-edit.txt"
    target.write_text("before", encoding="utf-8")
    result = await _call(
        EditFileTool(),
        workspace,
        assert_write_allowed=deny,
        path="fenced-edit.txt",
        old_string="before",
        new_string="after",
    )
    assert result.is_error and result.error_kind == "claim_fenced"
    assert target.read_text(encoding="utf-8") == "before"


@pytest.mark.asyncio
async def test_direct_write_requires_executor_authorization(workspace: Path) -> None:
    result = await _call(
        WriteFileTool(),
        workspace,
        approved=False,
        path="unauthorized.txt",
        content="nope",
    )
    assert result.is_error and result.error_kind == "approval_required"
    assert not (workspace / "unauthorized.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are POSIX-only")
@pytest.mark.asyncio
async def test_read_fifo_is_rejected(workspace: Path) -> None:
    fifo = workspace / "pipe"
    os.mkfifo(fifo)
    result = await _call(ReadFileTool(), workspace, path="pipe")
    assert result.is_error and result.error_kind == "invalid_argument"


@pytest.mark.asyncio
async def test_grep_does_not_read_sensitive_files(workspace: Path) -> None:
    (workspace / ".env").write_text("SECRET_TOKEN=leak", encoding="utf-8")
    result = await _call(GrepTool(), workspace, pattern="SECRET_TOKEN")
    assert not result.is_error
    assert "leak" not in result.content
    assert "no matches" in result.content
