"""External command hooks.

A command hook is an OS subprocess started with an argument vector, never a
shell. It receives one JSON object on stdin and writes at most one JSON object
on stdout. The environment is reduced to a small allowlist so provider keys and
other ambient secrets do not leak into hook processes.

Contract:
  exit 0 + empty stdout .......................... continue
  exit 0 + ``{"action": "continue", "reason"?}`` .. continue
  exit 0 + ``{"action": "deny", "reason"?}`` ...... deny (tool_before only)
  either of the above with ``context_append`` ..... failure; external hooks may
                                                    not modify model context
  non-zero exit / invalid JSON / output overflow .. failure (fail_mode applies)

Failure messages include only a bounded, redacted excerpt of the child's
output; raw stdout/stderr is never surfaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bear_agent.config.models import HookCommandConfig
from bear_agent.core.errors import HookError, HookTimeout
from bear_agent.hooks.models import HookDecision, HookInvocation
from bear_agent.secrets.redaction import SecretRedactor

STDOUT_LIMIT_BYTES = 64 * 1024
STDERR_LIMIT_BYTES = 64 * 1024
_EXCERPT_CHARS = 300

# Minimal environment. Provider credentials and other BEAR_*/user secrets are
# deliberately absent. Locale variables are included so UTF-8 handling stays
# predictable on POSIX.
ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LANGUAGE",
        "TZ",
    }
)


class HookCommandError(HookError):
    """The command hook failed to produce a valid decision."""


class HookCommandViolation(HookCommandError):
    """The command hook attempted something it is not permitted to do."""


def _minimal_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in ENV_ALLOWLIST}


def _excerpt(data: bytes, redact: Callable[[str], str]) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return "(no output)"
    return redact(text[:_EXCERPT_CHARS])


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            keep = limit - (total - len(chunk))
            if keep > 0:
                chunks.append(chunk[:keep])
            return b"".join(chunks), True
        chunks.append(chunk)
    return b"".join(chunks), False


async def _write_stdin(stream: asyncio.StreamWriter, data: bytes) -> None:
    try:
        stream.write(data)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        with contextlib.suppress(Exception):
            stream.close()


async def _collect(
    proc: asyncio.subprocess.Process,
    stdin_data: bytes,
    stdout_limit: int,
    stderr_limit: int,
) -> tuple[bytes, bool, bytes, bool, int]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_task = asyncio.create_task(_read_limited(proc.stdout, stdout_limit))
    stderr_task = asyncio.create_task(_read_limited(proc.stderr, stderr_limit))
    write_task = asyncio.create_task(_write_stdin(proc.stdin, stdin_data))
    try:
        stdout, stdout_overflow = await stdout_task
        # Stop reading at the bound. Kill the child immediately: otherwise it
        # blocks on a full pipe and ``proc.wait()`` would hang forever.
        if stdout_overflow:
            await _kill_tree(proc)
        stderr, stderr_overflow = await stderr_task
        if stderr_overflow:
            await _kill_tree(proc)
        await write_task
        returncode = await proc.wait()
    finally:
        for task in (stdout_task, stderr_task, write_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, write_task, return_exceptions=True)
    return stdout, stdout_overflow, stderr, stderr_overflow, returncode


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the hook process and its children; never wait indefinitely."""
    if proc.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/T",
                "/F",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
        except (OSError, ProcessLookupError, TimeoutError):
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
    else:
        kill_group = getattr(os, "killpg", None)
        get_group = getattr(os, "getpgid", None)
        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        if kill_group is not None and get_group is not None:
            try:
                kill_group(get_group(proc.pid), sigkill)
            except (ProcessLookupError, PermissionError, OSError):
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
        else:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5.0)


def _invocation_document(invocation: HookInvocation) -> bytes:
    document = {
        "event": invocation.event.value,
        "session_id": invocation.session_id,
        "run_id": invocation.run_id,
        "turn_id": invocation.turn_id,
        "payload": invocation.payload,
        "workspace": str(invocation.workspace),
        "hook_id": invocation.hook_id,
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


async def run_command_hook(
    command: HookCommandConfig,
    invocation: HookInvocation,
    *,
    workspace: Path,
    safe_cwd: Path | None = None,
    redactor: SecretRedactor | None = None,
    timeout_s: float | None = None,
) -> HookDecision:
    """Run one external command hook and validate its JSON decision."""
    redact = (redactor or SecretRedactor()).redact
    timeout = timeout_s if timeout_s is not None else command.timeout_s
    if timeout <= 0:
        timeout = command.timeout_s
    stdin_data = _invocation_document(invocation)
    popen_kwargs: dict[str, Any] = {}
    if os.name == "posix":
        # New session => the whole process group can be killed on timeout.
        popen_kwargs["start_new_session"] = True
    try:
        proc = await asyncio.create_subprocess_exec(
            command.command,
            *command.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(safe_cwd or Path.home()),
            env=_minimal_env(),
            **popen_kwargs,
        )
    except (OSError, ValueError) as exc:
        message = redact(str(exc))
        raise HookCommandError(f"cannot start hook command {command.id!r}: {message}") from exc

    try:
        stdout, stdout_overflow, stderr, _stderr_overflow, returncode = await asyncio.wait_for(
            _collect(proc, stdin_data, STDOUT_LIMIT_BYTES, STDERR_LIMIT_BYTES),
            timeout=timeout,
        )
    except TimeoutError:
        await asyncio.shield(_kill_tree(proc))
        raise HookTimeout(f"hook command {command.id!r} timed out after {timeout:g}s") from None
    except asyncio.CancelledError:
        await asyncio.shield(_kill_tree(proc))
        raise
    except BaseException:
        # A read/write error still leaves a live child behind.
        await asyncio.shield(_kill_tree(proc))
        raise

    if stdout_overflow:
        raise HookCommandError(
            f"hook command {command.id!r} exceeded {STDOUT_LIMIT_BYTES} bytes on stdout"
        )
    if returncode != 0:
        detail = _excerpt(stderr or stdout, redact)
        raise HookCommandError(
            f"hook command {command.id!r} exited with code {returncode}: {detail}"
        )

    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return HookDecision(action="continue")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        detail = _excerpt(stdout, redact)
        raise HookCommandError(
            f"hook command {command.id!r} returned invalid JSON: {detail}"
        ) from None
    if not isinstance(payload, dict):
        raise HookCommandError(f"hook command {command.id!r} must return a single JSON object")
    action = payload.get("action", "continue")
    if action not in ("continue", "deny"):
        raise HookCommandError(f"hook command {command.id!r} returned invalid action {action!r}")
    context_append = payload.get("context_append", "")
    if context_append:
        raise HookCommandViolation(
            f"hook command {command.id!r} attempted context_append; "
            "external hooks cannot modify model context"
        )
    raw_reason = payload.get("reason", "")
    reason = redact(raw_reason) if isinstance(raw_reason, str) else ""
    return HookDecision(action=action, reason=reason)


__all__ = [
    "ENV_ALLOWLIST",
    "STDOUT_LIMIT_BYTES",
    "HookCommandError",
    "HookCommandViolation",
    "run_command_hook",
]
