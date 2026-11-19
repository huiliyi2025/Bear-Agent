"""Real sandbox execution tests.

These run only when the platform sandbox actually works in the current
environment; otherwise they skip rather than pretending isolation passed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bear_agent.core.cancel import CancelToken
from bear_agent.shell.environment import build_shell_env
from bear_agent.shell.runner import ShellRunner
from bear_agent.shell.sandbox.base import SandboxPolicy
from bear_agent.shell.sandbox.linux_bwrap import BwrapSandbox


def _bwrap_works() -> bool:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False
    try:
        result = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--", "/bin/true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.mark.skipif(
    sys.platform != "linux" or not _bwrap_works(),
    reason="bubblewrap is not usable in this environment",
)
@pytest.mark.asyncio
async def test_bwrap_blocks_writes_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    sandbox = BwrapSandbox()
    policy = SandboxPolicy(workspace=workspace, network=False)
    runner = ShellRunner()
    command = "echo inside > inside.txt; echo outside > ../outside.txt"
    sandboxed = sandbox.wrap(
        ["/bin/sh", "-c", command],
        policy,
        workspace,
    )
    result = await runner.run(
        command,
        sandboxed,
        cwd=workspace,
        env=build_shell_env(_shell_config(), workspace),
        timeout_s=15,
        cancel=CancelToken(),
        max_output_bytes=10_000,
    )
    assert result.exit_code != 0
    assert (workspace / "inside.txt").exists()
    assert not outside.exists()

    command_ok = "echo inside > inside.txt"
    sandboxed_ok = sandbox.wrap(
        ["/bin/sh", "-c", command_ok],
        policy,
        workspace,
    )
    result_ok = await runner.run(
        command_ok,
        sandboxed_ok,
        cwd=workspace,
        env=build_shell_env(_shell_config(), workspace),
        timeout_s=15,
        cancel=CancelToken(),
        max_output_bytes=10_000,
    )
    assert result_ok.exit_code == 0
    assert (workspace / "inside.txt").read_text(encoding="utf-8").strip() == "inside"

    (workspace / ".env").write_text("SECRET=should-not-leak", encoding="utf-8")
    secret_command = "cat .env"
    secret_sandboxed = sandbox.wrap(
        ["/bin/sh", "-c", secret_command],
        policy,
        workspace,
    )
    secret_result = await runner.run(
        secret_command,
        secret_sandboxed,
        cwd=workspace,
        env=build_shell_env(_shell_config(), workspace),
        timeout_s=15,
        cancel=CancelToken(),
        max_output_bytes=10_000,
    )
    assert "should-not-leak" not in secret_result.stdout


def _shell_config():
    from bear_agent.config.models import ShellConfig

    return ShellConfig()
