"""Sandbox port and platform selection.

A sandbox decision says what containment was actually applied. It is never
enough to check command text. ``require`` demands filesystem isolation;
``process`` permits process/resource containment (Windows Job Object) but the
capabilities and audit record make the missing filesystem boundary explicit.
``off`` is an explicit opt-out and reports ``sandboxed=False``.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bear_agent.config.models import SandboxConfig
from bear_agent.core.errors import SandboxUnavailable


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    filesystem: bool = False
    network: bool = False
    process: bool = False
    resource: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "filesystem": self.filesystem,
            "network": self.network,
            "process": self.process,
            "resource": self.resource,
        }


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    workspace: Path
    writable_roots: tuple[Path, ...] = ()
    network: bool = False
    read_system: bool = True
    sensitive_paths: tuple[Path, ...] = ()


@dataclass
class SandboxedCommand:
    argv: list[str]
    backend: str
    sandboxed: bool
    capabilities: SandboxCapabilities
    note: str = ""
    on_spawn: Callable[[int], None] | None = None
    cleanup: Callable[[], None] | None = None
    # Windows Job Object assignment must happen before user code runs. The
    # runner creates the process suspended, calls on_spawn, then resume.
    suspend_on_spawn: bool = False
    resume: Callable[[int], None] | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class Sandbox(ABC):
    name: str
    capabilities: SandboxCapabilities

    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def wrap(self, argv: list[str], policy: SandboxPolicy, cwd: Path) -> SandboxedCommand:
        raise NotImplementedError


class NoSandbox(Sandbox):
    name = "none"
    capabilities = SandboxCapabilities()

    def available(self) -> bool:
        return True

    def wrap(self, argv: list[str], policy: SandboxPolicy, cwd: Path) -> SandboxedCommand:
        return SandboxedCommand(
            argv=list(argv),
            backend="none",
            sandboxed=False,
            capabilities=self.capabilities,
            note="sandbox explicitly disabled; command is not OS-isolated",
        )


class SandboxManager:
    def __init__(
        self,
        config: SandboxConfig,
        workspace: str | Path,
        *,
        platform: str | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.config = config
        self.workspace = Path(workspace).resolve()
        self.platform = platform or sys.platform
        self.backend = self._select_backend(which)
        self._sensitive_cache: tuple[float, tuple[Path, ...]] | None = None

    def _select_backend(self, which: Callable[[str], str | None]) -> Sandbox | None:
        candidate: Sandbox
        if self.platform.startswith("linux"):
            from bear_agent.shell.sandbox.linux_bwrap import BwrapSandbox

            candidate = BwrapSandbox(which=which)
            return candidate if candidate.available() else None
        if self.platform == "darwin":
            from bear_agent.shell.sandbox.macos_seatbelt import SeatbeltSandbox

            candidate = SeatbeltSandbox(which=which)
            return candidate if candidate.available() else None
        if self.platform == "win32":
            from bear_agent.shell.sandbox.windows_job import WindowsJobObjectSandbox

            candidate = WindowsJobObjectSandbox()
            return candidate if candidate.available() else None
        return None

    def ensure_available(self) -> Sandbox:
        if self.config.mode == "off":
            return NoSandbox()
        if self.backend is None:
            raise SandboxUnavailable(
                f"no OS sandbox backend is available on {self.platform}; "
                "shell execution is disabled (set sandbox.mode='off' explicitly "
                "to accept an unsandboxed run)"
            )
        if self.config.mode == "require" and not self.backend.capabilities.filesystem:
            raise SandboxUnavailable(
                f"sandbox backend {self.backend.name!r} does not isolate the "
                "filesystem; set sandbox.mode='process' to accept process-only "
                "containment, or 'off' to run unsandboxed"
            )
        if self.config.mode == "process" and not (
            self.backend.capabilities.process or self.backend.capabilities.resource
        ):
            raise SandboxUnavailable(
                f"sandbox backend {self.backend.name!r} provides no process containment"
            )
        return self.backend

    def wrap(self, argv: list[str], cwd: Path) -> SandboxedCommand:
        backend = self.ensure_available()
        policy = SandboxPolicy(
            workspace=self.workspace,
            writable_roots=tuple(Path(root).resolve() for root in self.config.writable_roots),
            network=self.config.network,
            read_system=self.config.read_system,
            sensitive_paths=(self._sensitive_paths() if backend.capabilities.filesystem else ()),
        )
        return backend.wrap(argv, policy, cwd)

    def _sensitive_paths(self) -> tuple[Path, ...]:
        now = time.monotonic()
        if self._sensitive_cache is not None and now - self._sensitive_cache[0] < 5.0:
            return self._sensitive_cache[1]
        from bear_agent.policy.path_guard import is_sensitive_path

        ignored = {
            ".git",
            ".bear",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            "dist",
            "build",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
        }
        found: list[Path] = []
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [name for name in dirs if name not in ignored]
            for name in files:
                path = Path(root) / name
                try:
                    hardlinked = path.is_file() and path.stat().st_nlink > 1
                except OSError:
                    hardlinked = False
                if is_sensitive_path(str(path))[0] or hardlinked:
                    found.append(path)
                    if len(found) > 1_000:
                        raise SandboxUnavailable(
                            "more than 1000 sensitive or hard-linked files in the "
                            "workspace; refusing to run a shell whose read surface "
                            "cannot be masked completely"
                        )
        result = tuple(found)
        self._sensitive_cache = (now, result)
        return result

    def describe(self) -> dict[str, object]:
        backend = self.backend
        return {
            "mode": self.config.mode,
            "platform": self.platform,
            "backend": backend.name if backend else "none",
            "capabilities": backend.capabilities.as_dict() if backend else {},
        }
