from __future__ import annotations

from pathlib import Path

import pytest

from bear_agent.policy.path_guard import (
    PathAccessError,
    PathGuard,
    is_sensitive_path,
    normalize_for_match,
)


def test_inside_workspace(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "app.py"
    target.write_text("x", encoding="utf-8")
    guard = PathGuard(tmp_path)
    assert guard.resolve("src/app.py") == target.resolve()
    assert guard.resolve(str(target)) == target.resolve()


def test_traversal_and_outside(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    guard = PathGuard(workspace)
    with pytest.raises(PathAccessError, match="outside workspace"):
        guard.resolve("../outside.txt")
    with pytest.raises(PathAccessError, match="outside workspace"):
        guard.resolve(str(outside))


def test_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "link"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this host")
    guard = PathGuard(workspace)
    with pytest.raises(PathAccessError, match="outside workspace"):
        guard.resolve("link/secret.txt")


def test_sensitive_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    guard = PathGuard(workspace)
    for name in (".env", ".env.local", "credentials.json", "id_rsa", "server.pem"):
        path = workspace / name
        path.write_text("secret", encoding="utf-8")
        with pytest.raises(PathAccessError, match="sensitive file"):
            guard.resolve(name)
    (workspace / "id_rsa.pub").write_text("public", encoding="utf-8")
    assert guard.resolve("id_rsa.pub").name == "id_rsa.pub"
    (workspace / ".env.example").write_text("X=1", encoding="utf-8")
    assert guard.resolve(".env.example").name == ".env.example"


def test_git_dir_write_blocked(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / "config").write_text("", encoding="utf-8")
    guard = PathGuard(workspace)
    with pytest.raises(PathAccessError, match=r"\.git"):
        guard.resolve(".git/config", mode="write")
    with pytest.raises(PathAccessError, match=r"\.git"):
        guard.resolve(".GIT/config", mode="write")
    assert guard.resolve(".git/config", mode="read")


def test_sensitive_detection_normalization() -> None:
    assert is_sensitive_path(".env")[0]
    assert is_sensitive_path("config/.env.production")[0]
    assert is_sensitive_path(".env.example")[0] is False
    assert is_sensitive_path("scripts/credentials.yaml")[0]
    assert is_sensitive_path("src/tokenizer.py")[0] is False
    assert is_sensitive_path(".env::$DATA")[0]
    assert normalize_for_match("C:\\work\\.env") == "c:/work/.env"


def test_relative_new_file_is_allowed(tmp_path: Path) -> None:
    guard = PathGuard(tmp_path)
    path = guard.resolve("new/dir/file.txt", mode="write")
    assert str(path).startswith(str(tmp_path.resolve()))
    assert not path.exists()
