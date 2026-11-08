"""Integration-style tests for the built-in tools (real files + real shell)."""

import pytest

from bear_agent.tools.builtin import builtin_tools


def test_builtin_names_and_permissions():
    tools = builtin_tools("/tmp")
    by_name = {t.name: t.permission for t in tools}
    assert by_name["read_file"] == "read_only"
    assert by_name["edit_file"] == "modify"
    assert by_name["write_file"] == "modify"
    assert by_name["run_shell"] == "dangerous"


def test_write_read_edit_roundtrip(tmp_path, builtin):
    result = builtin["write_file"].call(
        {"path": "hello.txt", "content": "line1\nline2\nline3\n"}
    )
    assert result.ok

    result = builtin["read_file"].call({"path": "hello.txt"})
    assert result.ok
    assert "line1" in result.content and "line3" in result.content
    assert "3 lines" in result.content

    result = builtin["edit_file"].call(
        {"path": "hello.txt", "old_string": "line2", "new_string": "edited-line"}
    )
    assert result.ok
    content = (tmp_path / "hello.txt").read_text(encoding="utf-8")
    assert "edited-line" in content and "line3" in content


def test_read_file_slice(tmp_path, builtin):
    (tmp_path / "big.txt").write_text(
        "\n".join(f"row{i}" for i in range(20)), encoding="utf-8"
    )
    result = builtin["read_file"].call({"path": "big.txt", "offset": 5, "limit": 3})
    assert result.ok
    assert "row5" in result.content and "row7" in result.content
    assert "row8" not in result.content


def test_read_missing_file(builtin):
    result = builtin["read_file"].call({"path": "nope.txt"})
    assert not result.ok
    assert "not found" in result.error


def test_edit_not_found_and_ambiguous(tmp_path, builtin):
    (tmp_path / "f.txt").write_text("same\nsame\n", encoding="utf-8")
    result = builtin["edit_file"].call(
        {"path": "f.txt", "old_string": "NOPE", "new_string": "x"}
    )
    assert not result.ok and "not found" in result.error

    result = builtin["edit_file"].call(
        {"path": "f.txt", "old_string": "same", "new_string": "x"}
    )
    assert not result.ok and "ambiguous" in result.error


def test_search_files_glob(tmp_path, builtin):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("x", encoding="utf-8")

    result = builtin["search_files"].call({"pattern": "**/*.py"})
    assert result.ok
    assert "a.py" in result.content and "c.py" in result.content
    assert "b.txt" not in result.content


def test_grep_files(tmp_path, builtin):
    (tmp_path / "mod.py").write_text(
        "def process(x):\n    return x\n", encoding="utf-8"
    )
    (tmp_path / "other.txt").write_text("nothing here\n", encoding="utf-8")

    result = builtin["grep_files"].call({"pattern": r"def process", "glob": "*.py"})
    assert result.ok
    assert "mod.py:1" in result.content

    result = builtin["grep_files"].call({"pattern": r"def process"})
    assert result.ok
    assert "other.txt" not in result.content


def test_grep_invalid_regex(builtin):
    result = builtin["grep_files"].call({"pattern": "["})
    assert not result.ok and "invalid regex" in result.error


@pytest.mark.parametrize(
    "command,expected", [("echo hello", True), ("echo oops && exit 3", False)]
)
def test_run_shell_exit_code_mapping(builtin, command, expected):
    result = builtin["run_shell"].call({"command": command})
    assert result.ok is expected


def test_run_shell_echoes_command_and_exit_code(builtin):
    result = builtin["run_shell"].call({"command": "echo hello"})
    assert result.ok
    assert "echo hello" in result.content
    assert "exit code 0" in result.content


def test_run_shell_timeout(builtin):
    result = builtin["run_shell"].call(
        {"command": "ping -n 30 127.0.0.1 >nul", "timeout": 1}
    )
    assert not result.ok
    assert "timed out" in result.error
