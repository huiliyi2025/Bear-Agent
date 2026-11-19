"""Windows Job Object containment.

This is a real OS-level process/resource boundary: children are assigned to a
job with kill-on-close, an active-process limit and memory limits. It does NOT
provide filesystem isolation. ``SandboxManager`` therefore rejects this backend
in ``require`` mode and labels it ``process-only`` in ``process`` mode.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from bear_agent.core.errors import SandboxUnavailable
from bear_agent.shell.sandbox.base import (
    Sandbox,
    SandboxCapabilities,
    SandboxedCommand,
    SandboxPolicy,
)

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_SUSPEND_RESUME = 0x0800
MAX_ACTIVE_PROCESSES = 64
PROCESS_MEMORY_LIMIT = 512 * 1024 * 1024
JOB_MEMORY_LIMIT = 1024 * 1024 * 1024


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJobObjectSandbox(Sandbox):
    name = "windows-job-object"
    capabilities = SandboxCapabilities(filesystem=False, network=False, process=True, resource=True)

    def available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            return hasattr(kernel32, "CreateJobObjectW")
        except (OSError, AttributeError):
            return False

    def wrap(self, argv: list[str], policy: SandboxPolicy, cwd: Path) -> SandboxedCommand:
        if not self.available():
            raise SandboxUnavailable("Windows Job Objects are not available")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise SandboxUnavailable(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        info.BasicLimitInformation.ActiveProcessLimit = MAX_ACTIVE_PROCESSES
        info.ProcessMemoryLimit = PROCESS_MEMORY_LIMIT
        info.JobMemoryLimit = JOB_MEMORY_LIMIT
        if not kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise SandboxUnavailable(f"SetInformationJobObject failed: {error}")

        closed = False

        def assign(pid: int) -> None:
            process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
            if not process:
                raise SandboxUnavailable(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
            try:
                if not kernel32.AssignProcessToJobObject(job, process):
                    raise SandboxUnavailable(
                        f"AssignProcessToJobObject({pid}) failed: {ctypes.get_last_error()}"
                    )
            finally:
                kernel32.CloseHandle(process)

        def cleanup() -> None:
            nonlocal closed
            if not closed:
                closed = True
                kernel32.CloseHandle(job)

        def resume(pid: int) -> None:
            # NtResumeProcess is the only stable way to resume a process whose
            # suspended thread handle is owned by subprocess.Popen.
            ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            ntdll.NtResumeProcess.restype = ctypes.c_long
            process = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
            if not process:
                raise SandboxUnavailable(
                    f"OpenProcess(resume {pid}) failed: {ctypes.get_last_error()}"
                )
            try:
                status = int(ntdll.NtResumeProcess(process))
                if status < 0:
                    raise SandboxUnavailable(f"NtResumeProcess({pid}) failed: status={status}")
            finally:
                kernel32.CloseHandle(process)

        return SandboxedCommand(
            argv=list(argv),
            backend=self.name,
            sandboxed=True,
            capabilities=self.capabilities,
            note=(
                "Windows Job Object: process-tree, process-count and memory "
                "containment only; filesystem is NOT isolated"
            ),
            on_spawn=assign,
            cleanup=cleanup,
            suspend_on_spawn=True,
            resume=resume,
            metadata={"filesystem_isolated": False},
        )
