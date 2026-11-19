"""Deterministic shell environment and shell selection.

The model cannot inject arbitrary environment variables. The environment is
built from a small allowlist plus explicit user configuration. Provider/API
secrets are not inherited by default.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from bear_agent.config.models import ShellConfig


def build_shell_env(config: ShellConfig, workspace: Path) -> dict[str, str]:
    workspace = workspace.resolve()
    env: dict[str, str] = {}
    for name in config.env_allowlist:
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update({key: value for key, value in config.env.items()})
    if "PATH" not in env:
        env["PATH"] = os.defpath
    home = workspace / ".bear" / "home"
    tmp = workspace / ".bear" / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    if sys.platform != "win32":
        env["TMPDIR"] = str(tmp)
    env.setdefault("TEMP", str(tmp))
    env.setdefault("TMP", str(tmp))
    return env


def shell_argv(config: ShellConfig, command: str, platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    if platform == "win32":
        if config.windows_shell == "cmd":
            shell = os.environ.get("COMSPEC") or "cmd.exe"
            return [shell, "/d", "/s", "/c", command]
        shell = shutil.which("pwsh") or shutil.which("powershell.exe") or "powershell.exe"
        return [shell, "-NoProfile", "-NonInteractive", "-Command", command]
    shell = config.posix_shell or ("/bin/bash" if Path("/bin/bash").exists() else "/bin/sh")
    return [shell, "-c", command]
