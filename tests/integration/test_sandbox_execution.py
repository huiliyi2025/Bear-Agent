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
    # Create the sensitive file before any wrap: bwrap creates missing mount
    # points on the writable bind (mode 444), which would pollute the host.
    (workspace / ".env").write_text("SECRET=should-not-leak", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    # /tmp is a private tmpfs in this sandbox: ../outside.txt is writable
    # inside but must not reach the host. A read-only system path must fail.
    system_target = Path("/usr") / f"bear-agent-outside-{tmp_path.name}.txt"
    sandbox = BwrapSandbox()
    # This test constructs the policy directly rather than through
    # SandboxManager, so it must declare the sensitive file it later masks.
    policy = SandboxPolicy(
        workspace=workspace,
        network=False,
        sensitive_paths=(workspace / ".env",),
    )
    runner = ShellRunner()
    command = (
        f"echo inside > inside.txt; echo outside > ../outside.txt; echo system > {system_target}"
    )
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
    assert not system_target.exists()

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
