"""Best-effort process and filesystem resource readings.

These are diagnostics only. A platform that cannot provide a value returns
``None`` instead of a fabricated zero; callers must show "unavailable".
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def process_memory_bytes() -> int | None:
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(ProcessMemoryCounters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            process = kernel32.GetCurrentProcess()
            if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.WorkingSetSize)
        except (OSError, AttributeError, ValueError):
            return None
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux reports KiB, macOS bytes.
        divisor = 1024 if sys.platform.startswith("linux") else 1
        return int(usage.ru_maxrss) * divisor
    except (ImportError, OSError, ValueError):
        return None


def cpu_time_seconds() -> float | None:
    try:
        times = os.times()
        return float(times.user + times.system)
    except (AttributeError, OSError, ValueError):
        return None


def disk_usage(path: str | Path) -> dict[str, int] | None:
    try:
        usage = shutil.disk_usage(str(path))
        return {
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
        }
    except OSError:
        return None


def directory_size(path: str | Path, *, max_files: int = 20_000) -> int | None:
    """Bounded recursive size; returns None when the walk fails."""
    total = 0
    seen = 0
    try:
        for root, _dirs, files in os.walk(str(path)):
            for name in files:
                seen += 1
                if seen > max_files:
                    return None
                total += (Path(root) / name).stat().st_size
    except OSError:
        return None
    return total


def file_size(path: str | Path) -> int | None:
    try:
        return Path(path).stat().st_size
    except OSError:
        return None
