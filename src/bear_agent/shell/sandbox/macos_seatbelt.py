"""macOS Seatbelt sandbox via ``sandbox-exec``.

The profile denies by default, allows process execution and broad system reads,
and limits writes to the workspace and temporary directories. Network is
denied unless the policy allows it. Seatbelt is deprecated by Apple but remains
the only practical built-in per-process confinement mechanism.
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

_SYSTEM_READS = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library",
    "/private/var",
    "/opt",
)


def _quote_subpath(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


class SeatbeltSandbox(Sandbox):
    name = "macos-seatbelt"
    capabilities = SandboxCapabilities(filesystem=True, network=True, process=True, resource=False)

    def __init__(self, which: Callable[[str], str | None] = shutil.which) -> None:
        self.sandbox_exec = which("sandbox-exec")

    def available(self) -> bool:
        return bool(self.sandbox_exec)

    def profile(self, policy: SandboxPolicy) -> str:
        workspace = _quote_subpath(policy.workspace)
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow signal)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow file-read-metadata)",
        ]
        if policy.read_system:
            reads = " ".join(f'(subpath "{_quote_subpath(Path(p))}")' for p in _SYSTEM_READS)
            lines.append(f"(allow file-read* {reads})")
        lines.append(
            f'(allow file-read* (subpath "{workspace}") (subpath "/private/tmp") (subpath "/tmp"))'
        )
        writes = [
            f'(subpath "{workspace}")',
            '(subpath "/private/tmp")',
            '(subpath "/tmp")',
        ]
        for root in policy.writable_roots:
            writes.append(f'(subpath "{_quote_subpath(root)}")')
        lines.append("(allow file-write* " + " ".join(writes) + ")")
        for sensitive in policy.sensitive_paths:
            lines.append(f'(deny file-read* (literal "{_quote_subpath(sensitive)}"))')
        if policy.network:
            lines.append("(allow network*)")
        return "\n".join(lines)

    def wrap(self, argv: list[str], policy: SandboxPolicy, cwd: Path) -> SandboxedCommand:
        if not self.sandbox_exec:
            raise SandboxUnavailable("sandbox-exec is not available")
        profile = self.profile(policy)
        return SandboxedCommand(
            argv=[self.sandbox_exec, "-p", profile, "--", *argv],
            backend=self.name,
            sandboxed=True,
            capabilities=self.capabilities,
            note="Seatbelt: deny-by-default, workspace writes only",
            metadata={"network": policy.network},
        )
