"""Asynchronous shell process runner.

Responsibilities: spawn, capture bounded stdout/stderr, enforce timeout,
cooperate with cancellation, and kill the whole process tree. The sandbox
decision may attach spawn/cleanup hooks (Windows Job Object); failure of a hook
prevents the command from being considered successful.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from bear_agent.core.cancel import CancelToken, RunCancelled
from bear_agent.shell.sandbox.base import SandboxedCommand

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004


@dataclass
class ShellResult:
    command: str
    stdout: str
    stderr: str
    exit_code: int | None
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    sandbox_backend: str = "none"
    sandboxed: bool = False
    sandbox_capabilities: dict[str, bool] = field(default_factory=dict)
    sandbox_note: str = ""
    error: str | None = None


class ShellRunner:
    def __init__(self, *, grace_seconds: float = 2.0) -> None:
        self.grace_seconds = grace_seconds

    async def run(
        self,
        command: str,
        sandboxed: SandboxedCommand,
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_s: float,
        cancel: CancelToken,
        max_output_bytes: int,
    ) -> ShellResult:
        started = time.monotonic()
        backend = sandboxed.backend
        capabilities = sandboxed.capabilities.as_dict()
        if sandboxed.suspend_on_spawn and (sandboxed.on_spawn is None or sandboxed.resume is None):
            _cleanup_sandbox(sandboxed)
            return ShellResult(
                command=command,
                stdout="",
                stderr="",
                exit_code=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                sandbox_backend=backend,
                sandboxed=sandboxed.sandboxed,
                sandbox_capabilities=capabilities,
                sandbox_note=sandboxed.note,
                error="sandbox requested a suspended spawn without assign/resume hooks",
            )
        try:
            proc = await self._spawn(
                sandboxed.argv,
                cwd=cwd,
                env=env,
                suspend=sandboxed.suspend_on_spawn,
            )
        except FileNotFoundError as exc:
            _cleanup_sandbox(sandboxed)
            return ShellResult(
                command=command,
                stdout="",
                stderr="",
                exit_code=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                sandbox_backend=backend,
                sandboxed=sandboxed.sandboxed,
                sandbox_capabilities=capabilities,
                sandbox_note=sandboxed.note,
                error=f"executable not found: {exc}",
            )
        except OSError as exc:
            _cleanup_sandbox(sandboxed)
            return ShellResult(
                command=command,
                stdout="",
                stderr="",
                exit_code=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                sandbox_backend=backend,
                sandboxed=sandboxed.sandboxed,
                sandbox_capabilities=capabilities,
                sandbox_note=sandboxed.note,
                error=f"failed to start process: {exc}",
            )

        try:
            if sandboxed.on_spawn is not None:
                sandboxed.on_spawn(proc.pid)
            if sandboxed.resume is not None:
                sandboxed.resume(proc.pid)
            if cancel.is_cancelled:
                raise RunCancelled("run cancelled before command wait")
            out_task = asyncio.create_task(_read_bounded(proc.stdout, max_output_bytes))
            err_task = asyncio.create_task(_read_bounded(proc.stderr, max_output_bytes))
            wait_task = asyncio.create_task(proc.wait())
            cancel_task = asyncio.create_task(cancel.wait())
            timed_out = False
            cancelled = False
            try:
                done, _pending = await asyncio.wait(
                    {wait_task, cancel_task},
                    timeout=timeout_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    timed_out = True
                    await self._terminate(proc, sandboxed)
                elif cancel_task in done and not wait_task.done():
                    cancelled = True
                    await self._terminate(proc, sandboxed)
            finally:
                for task in (cancel_task,):
                    if not task.done():
                        task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task
                with contextlib.suppress(Exception):
                    await wait_task
            stdout, out_truncated = await _await_reader(out_task)
            stderr, err_truncated = await _await_reader(err_task)
            exit_code = proc.returncode
            return ShellResult(
                command=command,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=timed_out,
                cancelled=cancelled,
                truncated=out_truncated or err_truncated,
                sandbox_backend=backend,
                sandboxed=sandboxed.sandboxed,
                sandbox_capabilities=capabilities,
                sandbox_note=sandboxed.note,
            )
        except asyncio.CancelledError:
            await self._terminate(proc, sandboxed)
            raise
        except RunCancelled:
            await self._terminate(proc, sandboxed)
            return ShellResult(
                command=command,
                stdout="",
                stderr="",
                exit_code=proc.returncode,
                duration_ms=int((time.monotonic() - started) * 1000),
                cancelled=True,
                sandbox_backend=backend,
                sandboxed=sandboxed.sandboxed,
                sandbox_capabilities=capabilities,
                sandbox_note=sandboxed.note,
            )
        except Exception as exc:
            await self._terminate(proc, sandboxed)
            return ShellResult(
                command=command,
                stdout="",
                stderr="",
                exit_code=proc.returncode,
                duration_ms=int((time.monotonic() - started) * 1000),
                sandbox_backend=backend,
                sandboxed=sandboxed.sandboxed,
                sandbox_capabilities=capabilities,
                sandbox_note=sandboxed.note,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            _cleanup_sandbox(sandboxed)

    async def _spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        suspend: bool = False,
    ) -> asyncio.subprocess.Process:
        kwargs: dict[str, object] = {
            "cwd": str(cwd),
            "env": env,
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if sys.platform == "win32":
            creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            if suspend:
                creationflags |= CREATE_SUSPENDED
            kwargs["creationflags"] = creationflags
            return await asyncio.create_subprocess_exec(
                *argv,
                **kwargs,  # type: ignore[arg-type]
            )
        return await asyncio.create_subprocess_exec(
            *argv,
            **kwargs,
            start_new_session=True,
        )

    async def _terminate(
        self, proc: asyncio.subprocess.Process, sandboxed: SandboxedCommand
    ) -> None:
        if proc.returncode is not None:
            if sandboxed.cleanup is not None:
                with contextlib.suppress(Exception):
                    sandboxed.cleanup()
            return
        if sys.platform == "win32":
            # Closing the job kills the tree; taskkill is a fallback for the
            # unsandboxed case and for children that escaped the job.
            if sandboxed.cleanup is not None:
                with contextlib.suppress(Exception):
                    sandboxed.cleanup()
            with contextlib.suppress(Exception):
                taskkill = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/T",
                    "/F",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await taskkill.wait()
            with contextlib.suppress(Exception):
                proc.kill()
        else:
            descendants = await _descendant_pids(proc.pid)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            for pid in descendants:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.grace_seconds)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                for pid in descendants:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=self.grace_seconds)


def _cleanup_sandbox(sandboxed: SandboxedCommand) -> None:
    if sandboxed.cleanup is not None:
        with contextlib.suppress(Exception):
            sandboxed.cleanup()


async def _descendant_pids(root_pid: int) -> list[int]:
    """Best-effort descendant scan for POSIX process-tree termination.

    A child that calls setsid leaves the process group; killpg alone would miss
    it. ``ps`` is present on macOS and mainstream Linux. The scan is advisory:
    the caller still kills the group and, on Windows, uses a Job Object.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-eo",
            "pid=,ppid=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (OSError, TimeoutError):
        return []
    children: dict[int, list[int]] = {}
    for line in stdout.decode("utf-8", errors="ignore").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    result: list[int] = []
    stack = list(children.get(root_pid, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
        stack.extend(children.get(pid, []))
    return result


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> tuple[str, bool]:
    if stream is None:
        return "", False
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        if total < limit:
            take = min(limit - total, len(chunk))
            chunks.append(chunk[:take])
            total += take
            if take < len(chunk):
                truncated = True
        else:
            truncated = True
    return b"".join(chunks).decode("utf-8", errors="replace"), truncated


async def _await_reader(task: asyncio.Task[tuple[str, bool]]) -> tuple[str, bool]:
    try:
        return await asyncio.wait_for(task, timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
        return "", True
