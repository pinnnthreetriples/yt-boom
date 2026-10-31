from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

from loguru import logger

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

_job_handle: int | None = None  # kept for the whole process lifetime: closing it kills the job's processes


class JobBasicLimits(ctypes.Structure):
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


class JobIoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class JobExtendedLimits(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION."""

    _fields_ = [
        ("BasicLimitInformation", JobBasicLimits),
        ("IoInfo", JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def configure_kill_on_close_job(kernel32: Any) -> int:
    """Create a KILL_ON_JOB_CLOSE job through `kernel32` and put the current process into it; returns the job handle.

    Raises OSError (the job handle is closed first) when any step fails.
    """
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = JobExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError:
        kernel32.CloseHandle(job)
        raise
    return int(job)


def bind_to_kill_on_close_job() -> bool:
    """Put this process into a Windows Job Object that kills all its child processes (firefox.exe) when it exits.

    Children inherit the job, so a crashed or killed script cannot leave browsers holding `parent.lock`.
    Returns True once the process is bound (also on repeated calls); no-op on other platforms.
    """
    global _job_handle
    if sys.platform != "win32":
        return False
    if _job_handle is not None:
        return True
    try:
        _job_handle = configure_kill_on_close_job(ctypes.WinDLL("kernel32", use_last_error=True))
    except OSError as exc:
        logger.warning("Не удалось привязать процесс к Windows Job Object: {}", exc)
        return False
    logger.debug("Процесс привязан к Job Object (KILL_ON_JOB_CLOSE)")
    return True


__all__ = ["bind_to_kill_on_close_job", "configure_kill_on_close_job"]
