from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from bear_agent.config.models import SandboxConfig
from bear_agent.core.errors import SandboxUnavailable
from bear_agent.shell.sandbox.base import (
    NoSandbox,
    SandboxManager,
    SandboxPolicy,
)
from bear_agent.shell.sandbox.linux_bwrap import BwrapSandbox
from bear_agent.shell.sandbox.macos_seatbelt import SeatbeltSandbox
from bear_agent.shell.sandbox.windows_job import WindowsJobObjectSandbox


def test_bwrap_command_builder(tmp_path: Path) -> None:
    sandbox = BwrapSandbox(which=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    assert sandbox.available()
    wrapped = sandbox.wrap(
        ["/bin/sh", "-c", "echo hi"],
        SandboxPolicy(workspace=tmp_path, network=False),
        tmp_path,
    )
    assert wrapped.sandboxed
    assert "--die-with-parent" in wrapped.argv
    assert "--unshare-net" in wrapped.argv
    assert "--bind" in wrapped.argv
    assert wrapped.argv[-3:] == ["--", "/bin/sh", "-c"] or wrapped.argv[-2:] == [
        "-c",
        "echo hi",
    ]
    assert str(tmp_path) in wrapped.argv


def test_bwrap_masks_sensitive_workspace_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SECRET=", encoding="utf-8")
    try:
        os.link(tmp_path / ".env", tmp_path / "alias.txt")
    except OSError:
        alias_available = False
    else:
        alias_available = True
    manager = SandboxManager(
        SandboxConfig(mode="require"),
        tmp_path,
        platform="linux",
        which=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    wrapped = manager.wrap(["/bin/sh", "-c", "true"], tmp_path)
    assert "--ro-bind-try" in wrapped.argv
    assert "/dev/null" in wrapped.argv
    assert any(str(tmp_path / ".env") == arg for arg in wrapped.argv)
    assert not any(str(tmp_path / ".env.example") == arg for arg in wrapped.argv)
    if alias_available:
        assert any(str(tmp_path / "alias.txt") == arg for arg in wrapped.argv)


def test_bwrap_network_policy(tmp_path: Path) -> None:
    sandbox = BwrapSandbox(which=lambda _: "/usr/bin/bwrap")
    wrapped = sandbox.wrap(
        ["/bin/sh", "-c", "true"],
        SandboxPolicy(workspace=tmp_path, network=True),
        tmp_path,
    )
    assert "--unshare-net" not in wrapped.argv


def test_seatbelt_profile_denies_and_scopes_writes(tmp_path: Path) -> None:
    sandbox = SeatbeltSandbox(which=lambda _: "/usr/bin/sandbox-exec")
    assert sandbox.available()
    profile = sandbox.profile(SandboxPolicy(workspace=tmp_path, network=False))
    assert "(deny default)" in profile
    assert "(allow file-write*" in profile
    assert str(tmp_path).replace("\\", "\\\\") in profile
    assert "(allow network*)" not in profile


def test_seatbelt_profile_denies_sensitive_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    sandbox = SeatbeltSandbox(which=lambda _: "/usr/bin/sandbox-exec")
    manager_policy = SandboxPolicy(
        workspace=tmp_path,
        sensitive_paths=(tmp_path / ".env",),
    )
    profile = sandbox.profile(manager_policy)
    assert '(deny file-read* (literal "' in profile
    assert ".env" in profile


def test_no_sandbox_is_explicitly_unisolated(tmp_path: Path) -> None:
    wrapped = NoSandbox().wrap(
        ["/bin/sh", "-c", "true"], SandboxPolicy(workspace=tmp_path), tmp_path
    )
    assert wrapped.sandboxed is False
    assert wrapped.backend == "none"


def test_require_mode_fails_closed_without_filesystem_backend(
    tmp_path: Path,
) -> None:
    manager = SandboxManager(
        SandboxConfig(mode="require"),
        tmp_path,
        platform="win32",
        which=lambda _: None,
    )
    if sys.platform == "win32":
        # The Windows Job Object exists but does not isolate the filesystem.
        with pytest.raises(SandboxUnavailable, match="filesystem"):
            manager.ensure_available()
    else:
        with pytest.raises(SandboxUnavailable):
            manager.ensure_available()


def test_off_mode_returns_no_sandbox(tmp_path: Path) -> None:
    manager = SandboxManager(
        SandboxConfig(mode="off"), tmp_path, platform="linux", which=lambda _: None
    )
    wrapped = manager.wrap(["/bin/sh", "-c", "true"], tmp_path)
    assert not wrapped.sandboxed


def test_manager_selects_bwrap_on_linux(tmp_path: Path) -> None:
    manager = SandboxManager(
        SandboxConfig(mode="require"),
        tmp_path,
        platform="linux",
        which=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    assert manager.backend is not None
    assert manager.describe()["backend"] == "linux-bwrap"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object only")
def test_windows_job_object_available(tmp_path: Path) -> None:
    sandbox = WindowsJobObjectSandbox()
    assert sandbox.available()
    wrapped = sandbox.wrap(
        ["cmd.exe", "/c", "echo", "x"], SandboxPolicy(workspace=tmp_path), tmp_path
    )
    assert wrapped.sandboxed
    assert wrapped.capabilities.process and wrapped.capabilities.resource
    assert not wrapped.capabilities.filesystem
    assert wrapped.on_spawn is not None and wrapped.cleanup is not None
    assert wrapped.suspend_on_spawn and wrapped.resume is not None
    wrapped.cleanup()
