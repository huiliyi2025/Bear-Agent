"""Process registry and conservative orphan termination.

Shell processes are registered in SQLite before a run can outlive them. After a
crash the recovery service asks the registry to terminate leftovers, but a PID
alone is not a safe handle because PIDs are reused. The default terminator
therefore verifies the OS-reported process start time against the registration
timestamp before signalling, and refuses to act when identity cannot be
established.

POSIX group termination is used only when the recorded ``pgid`` equals the pid
and the process is still its own group leader. On Windows the creation time
from ``GetProcessTimes`` is compared before ``TerminateProcess`` / ``taskkill``.
The terminator never signals PID 0/1, the current process or the current
process group.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import os
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from bear_agent.audit.service import AuditService
from bear_agent.core.clock import Clock, SystemClock
from bear_agent.core.errors import SessionLeaseError
from bear_agent.core.events import EventType
from bear_agent.core.fencing import LeaseFence
from bear_agent.core.ids import new_id
from bear_agent.observability.recorder import EventRecorder
from bear_agent.storage.continuity_repositories import ProcessRecord, ProcessRepository
from bear_agent.storage.repositories import assert_session_lease_row

logger = logging.getLogger(__name__)

# Registration happens milliseconds after spawn. A wide window would let a
# reused PID pass identity verification; keep it tight enough for ps lstart's
# 1-second resolution while tolerating normal spawn/DB latency.
IDENTITY_TOLERANCE_S = 2.0
KILL_GRACE_S = 2.0

_WINDOWS_PROCESS_TERMINATE = 0x0001
_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True, slots=True)
class TerminationResult:
    terminated: bool
    reason: str
    pid: int


class ProcessTerminator(Protocol):
    async def terminate(self, record: ProcessRecord) -> TerminationResult: ...


class DefaultTerminator:
    """Cross-platform, identity-checked process terminator.

    Every failure path returns ``terminated=False`` with a reason. A process is
    only signalled after its start time matches the registration time within
    :data:`IDENTITY_TOLERANCE_S`; if that cannot be established the terminator
    does nothing.
    """

    def __init__(
        self,
        *,
        platform: str | None = None,
        tolerance_s: float = IDENTITY_TOLERANCE_S,
        grace_s: float = KILL_GRACE_S,
    ) -> None:
        self.platform = platform or sys.platform
        self.tolerance_s = tolerance_s
        self.grace_s = grace_s

    async def terminate(self, record: ProcessRecord) -> TerminationResult:
        pid = record.pid
        if pid <= 1:
            return TerminationResult(False, "refused_pid", pid)
        if pid == os.getpid():
            return TerminationResult(False, "refused_self", pid)
        if self.platform == "win32":
            return await self._terminate_windows(record)
        return await self._terminate_posix(record)

    # ------------------------------------------------------------------- POSIX

    async def _terminate_posix(self, record: ProcessRecord) -> TerminationResult:
        pid = record.pid
        if not _process_exists(pid):
            return TerminationResult(False, "not_running", pid)
        started = await _process_start_time(pid, self.platform)
        if started is None:
            return TerminationResult(False, "identity_unverified", pid)
        if abs((record.created_at - started).total_seconds()) > self.tolerance_s:
            return TerminationResult(False, "identity_mismatch", pid)

        group = record.pgid is not None and record.pgid == pid and _safe_getpgid(pid) == pid
        own_group = _own_process_group()
        if group and own_group is not None and record.pgid == own_group:
            # Signalling our own process group would kill the harness.
            group = False
        kill_group = _kill_group()
        if group and kill_group is None:
            group = False
        try:
            if group and kill_group is not None:
                kill_group(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return TerminationResult(False, "not_running", pid)
        except PermissionError:
            return TerminationResult(False, "permission_denied", pid)
        except OSError as exc:
            return TerminationResult(False, f"signal_failed: {exc}", pid)

        deadline = asyncio.get_running_loop().time() + self.grace_s
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
            if not _process_exists(pid):
                return TerminationResult(
                    True, "terminated_group" if group else "terminated_pid", pid
                )
        try:
            if group and kill_group is not None:
                kill_group(pid, _sigkill())
            else:
                os.kill(pid, _sigkill())
        except ProcessLookupError:
            pass
        except PermissionError:
            return TerminationResult(False, "permission_denied", pid)
        except OSError as exc:
            return TerminationResult(False, f"kill_failed: {exc}", pid)
        return TerminationResult(True, "killed_group" if group else "killed_pid", pid)

    # ----------------------------------------------------------------- Windows

    async def _terminate_windows(self, record: ProcessRecord) -> TerminationResult:
        pid = record.pid
        handle, created, reason = _windows_open_process(pid)
        if not handle:
            return TerminationResult(False, reason or "identity_unverified", pid)
        kernel32 = _kernel32()
        try:
            if created is None or kernel32 is None:
                return TerminationResult(False, "identity_unverified", pid)
            if abs((record.created_at - created).total_seconds()) > self.tolerance_s:
                return TerminationResult(False, "identity_mismatch", pid)
            # Prefer the tree kill: TerminateProcess only kills the recorded
            # shell, leaving npm/node/esbuild children behind. taskkill is
            # attempted first and TerminateProcess remains the fallback.
            if await _taskkill_tree(pid):
                return TerminationResult(True, "terminated_tree", pid)
            try:
                terminated = bool(kernel32.TerminateProcess(handle, 1))
            except Exception:  # pragma: no cover - ctypes failure
                terminated = False
            if terminated:
                return TerminationResult(True, "terminated_pid", pid)
            return TerminationResult(False, "terminate_failed", pid)
        finally:
            with contextlib.suppress(Exception):
                kernel32.CloseHandle(handle)


class ProcessRegistry:
    """Thin async facade over :class:`ProcessRepository`."""

    def __init__(
        self,
        repository: ProcessRepository,
        *,
        clock: Clock | None = None,
        terminator: ProcessTerminator | None = None,
        recorder: EventRecorder | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock or SystemClock()
        self.terminator = terminator or DefaultTerminator()
        self.recorder = recorder
        self.audit = audit

    async def register(
        self,
        session_id: str,
        tool_call_id: str | None,
        lease_owner: str,
        pid: int,
        pgid: int | None,
        command_preview: str | None,
        *,
        fence: LeaseFence | None = None,
    ) -> ProcessRecord:
        record = ProcessRecord(
            id=new_id("proc"),
            session_id=session_id,
            tool_call_id=tool_call_id,
            lease_owner=lease_owner,
            pid=pid,
            pgid=pgid,
            command_preview=command_preview,
            created_at=self.clock.now(),
            finished_at=None,
            status="running",
        )
        return await self.repository.create(record, fence=fence)

    async def mark_exited(self, record_id: str, *, fence: LeaseFence | None = None) -> None:
        await self.repository.finish(record_id, "exited", fence=fence)

    async def mark_terminated(self, record_id: str, *, fence: LeaseFence | None = None) -> None:
        await self.repository.finish(record_id, "terminated", fence=fence)

    async def running(self, session_id: str | None = None) -> list[ProcessRecord]:
        return await self.repository.list_running(session_id)

    async def terminate_orphans(
        self,
        session_id: str,
        *,
        lease_owner: str,
        fence: LeaseFence | None = None,
    ) -> list[TerminationResult]:
        """Terminate every process still registered as running for a session.

        Each attempt - successful or refused - is recorded as one
        ``recovery.process.terminated`` event and one audit record. A refused
        attempt leaves the row with status ``unknown`` instead of pretending it
        exited.
        """
        results: list[TerminationResult] = []
        for record in await self.repository.list_running(session_id):
            if record.lease_owner == lease_owner:
                # This is the current owner's live process, not an orphan.
                # Recovery must never kill work started by the lease holder.
                continue
            claimed = await self.repository.claim_termination(
                record.id, session_id, lease_owner, fence=fence
            )
            if not claimed:
                # Another recoverer won the transition, or the row changed
                # state between the read and the claim. Never signal twice.
                continue
            if fence is not None:
                # Narrow the claim-to-signal window: if the lease was taken
                # over after the claim, do not signal. The row stays "unknown"
                # and is reported rather than silently treated as cleaned.
                try:
                    await self.repository.db.run(
                        lambda conn: assert_session_lease_row(conn, session_id, fence)
                    )
                except SessionLeaseError:
                    results.append(TerminationResult(False, "fenced_before_signal", record.pid))
                    continue
            result = await self.terminator.terminate(record)
            try:
                await self.repository.record_outcome(
                    record.id,
                    "terminated" if result.terminated else "unknown",
                    fence=fence,
                )
            except SessionLeaseError:
                # The process was already signalled; the row stays "unknown",
                # which is truthful. Do not turn this into a false success.
                logger.warning(
                    "process record %s status could not be updated after signal",
                    record.id,
                )
            payload = {
                "session_id": session_id,
                "process_id": record.id,
                "pid": record.pid,
                "previous_owner": record.lease_owner,
                "terminated": result.terminated,
                "reason": result.reason,
                "lease_owner": lease_owner,
            }
            if self.recorder is not None:
                await self.recorder.record(
                    session_id,
                    EventType.RECOVERY_PROCESS_TERMINATED,
                    payload=payload,
                    fence=fence,
                )
            if self.audit is not None:
                await self.audit.append(
                    actor="agent",
                    action="recovery.process.terminated",
                    resource=record.id,
                    detail=payload,
                    session_id=session_id,
                )
            results.append(result)
        return results


# --------------------------------------------------------------------- helpers


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _safe_getpgid(pid: int) -> int | None:
    getpgid = getattr(os, "getpgid", None)
    if getpgid is None:
        return None
    try:
        return int(getpgid(pid))
    except (OSError, ValueError):
        return None


def _own_process_group() -> int | None:
    getpgrp = getattr(os, "getpgrp", None)
    if getpgrp is None:
        return None
    try:
        return int(getpgrp())
    except (OSError, ValueError):
        return None


def _kill_group() -> Any:
    """Return ``os.killpg`` on POSIX; ``None`` on platforms without it."""
    return getattr(os, "killpg", None)


def _sigkill() -> int:
    return int(getattr(signal, "SIGKILL", signal.SIGTERM))


async def _process_start_time(pid: int, platform: str) -> datetime | None:
    if platform.startswith("linux"):
        return _linux_start_time(pid)
    return await _ps_start_time(pid)


def _linux_start_time(pid: int) -> datetime | None:
    """Compute a process start wall time from ``/proc``.

    ``/proc/<pid>/stat`` field 22 is the start time in clock ticks since boot;
    the boot time comes from ``/proc/stat`` (``btime``).
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        # The comm field may contain spaces and parentheses, so split after the
        # last ')'. The remaining list starts at field 3 (state).
        fields = stat.rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])
        btime = _linux_boot_time()
        if btime is None:
            return None
        sysconf = getattr(os, "sysconf", None)
        if sysconf is None:
            return None
        clock_ticks = int(sysconf("SC_CLK_TCK"))
        return datetime.fromtimestamp(btime + start_ticks / clock_ticks, tz=UTC)
    except (OSError, ValueError, IndexError):
        return None


def _linux_boot_time() -> int | None:
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


async def _ps_start_time(pid: int) -> datetime | None:
    """Start time via ``ps -o lstart=`` (macOS and other POSIX systems)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-o",
            "lstart=",
            "-p",
            str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (OSError, TimeoutError):
        return None
    text = " ".join(stdout.decode("utf-8", errors="ignore").split())
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    # ps reports local time; make it timezone-aware for comparison.
    return parsed.astimezone()


class _WindowsFileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


def _kernel32() -> Any:
    """Return the kernel32 DLL handle on Windows, or ``None`` elsewhere."""
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        return None
    try:
        library = win_dll("kernel32", use_last_error=True)
    except OSError:
        return None
    library.OpenProcess.restype = ctypes.c_void_p
    library.OpenProcess.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    library.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_WindowsFileTime),
        ctypes.POINTER(_WindowsFileTime),
        ctypes.POINTER(_WindowsFileTime),
        ctypes.POINTER(_WindowsFileTime),
    ]
    library.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    return library


def _windows_open_process(pid: int) -> tuple[int | None, datetime | None, str]:
    """Open a process and read its creation time.

    Returns ``(handle, creation_time, reason)``. A ``None`` handle means the
    process could not be opened; the caller must not signal anything.
    """
    kernel32 = _kernel32()
    if kernel32 is None:
        return None, None, "unsupported_platform"
    handle = kernel32.OpenProcess(
        _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION | _WINDOWS_PROCESS_TERMINATE,
        0,
        pid,
    )
    if not handle:
        return None, None, "open_failed"
    creation = _WindowsFileTime()
    exit_time = _WindowsFileTime()
    kernel_time = _WindowsFileTime()
    user_time = _WindowsFileTime()
    ok = kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    )
    if not ok:
        with contextlib.suppress(Exception):
            kernel32.CloseHandle(handle)
        return None, None, "identity_unverified"
    return int(handle), _filetime_to_datetime(creation), ""


def _filetime_to_datetime(value: _WindowsFileTime) -> datetime:
    ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
    return datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=ticks / 10)


async def _taskkill_tree(pid: int) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "taskkill",
            "/T",
            "/F",
            "/PID",
            str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        code = await asyncio.wait_for(proc.wait(), timeout=5.0)
    except (OSError, TimeoutError):
        return False
    return code == 0
