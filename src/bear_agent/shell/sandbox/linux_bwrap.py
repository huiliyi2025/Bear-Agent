"""Linux bubblewrap sandbox.

Filesystem policy: system directories are read-only, the workspace is writable,
``/tmp`` is a private tmpfs. Network is removed with ``--unshare-net`` unless
the policy allows it. ``--die-with-parent`` prevents orphan leakage.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from bear_agent.core.errors import SandboxUnavailable
from bear_agent.shell.sandbox.base import (
    Sandbox,
    SandboxCapabilities,
    SandboxedCommand,
    SandboxPolicy,
)

_SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")


class BwrapSandbox(Sandbox):
    name = "linux-bwrap"
    capabilities = SandboxCapabilities(filesystem=True, network=True, process=True, resource=False)

    def __init__(self, which: Callable[[str], str | None] = shutil.which) -> None:
        self.bwrap = which("bwrap")

    def available(self) -> bool:
        return bool(self.bwrap)

    def wrap(self, argv: list[str], policy: SandboxPolicy, cwd: Path) -> SandboxedCommand:
        if not self.bwrap:
            raise SandboxUnavailable("bubblewrap (bwrap) is not installed")
        workspace = policy.workspace
        command: list[str] = [
            self.bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        if policy.read_system:
            for root in _SYSTEM_ROOTS:
                if Path(root).exists():
                    command.extend(["--ro-bind", root, root])
        command.extend(["--bind", str(workspace), str(workspace)])
        for sensitive in policy.sensitive_paths:
            command.extend(["--ro-bind-try", "/dev/null", str(sensitive)])
        for extra_root in policy.writable_roots:
            command.extend(["--bind", str(extra_root), str(extra_root)])
        command.extend(["--chdir", str(cwd)])
        if not policy.network:
            command.append("--unshare-net")
        command.append("--")
        command.extend(argv)
        return SandboxedCommand(
            argv=command,
            backend=self.name,
            sandboxed=True,
            capabilities=self.capabilities,
            note="bwrap: system read-only, workspace writable, private /tmp",
            metadata={"network": policy.network},
        )
