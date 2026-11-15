"""Windows details of running Claude Code (plan 4.4).

- No console window: Claude Code is started with CREATE_NO_WINDOW, so nothing flashes on screen even though ClaudeIn
  itself has no console (pythonw).
- Everything stops together: each run's Claude Code goes into its own job object set to kill every program in it when
  the job is closed. Stopping a run, or ClaudeIn ending (even by a crash), leaves nothing running.
- Keep awake: while runs are going or waiting, Windows is asked not to sleep.
"""

from __future__ import annotations

import contextvars
import ctypes
import os
import subprocess
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"

#: The job the Claude Code started in this context should join (set by the runner around each run).
current_job: contextvars.ContextVar["Job | None"] = contextvars.ContextVar("claudein_job", default=None)

if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.SetThreadExecutionState.restype = wintypes.DWORD
    _k32.SetThreadExecutionState.argtypes = [wintypes.DWORD]

_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class Job:
    """A Windows job object that kills everything in it when closed. A no-op elsewhere."""

    def __init__(self):
        self.handle = None
        if not IS_WINDOWS:
            return
        h = _k32.CreateJobObjectW(None, None)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(h, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                            ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            _k32.CloseHandle(h)
            raise ctypes.WinError(err)
        self.handle = h

    def add(self, pid: int) -> None:
        if not self.handle:
            return
        ph = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not ph:
            return  # it has already ended
        try:
            _k32.AssignProcessToJobObject(self.handle, ph)
        finally:
            _k32.CloseHandle(ph)

    def close(self) -> None:
        """Ends every program in the job, then lets the job go."""
        if self.handle:
            _k32.TerminateJobObject(self.handle, 1)
            _k32.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        self.close()


class _AnyioWithNoWindow:
    """Stands in for the SDK's `anyio` module: the same, except processes start with no window and join the run's job."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def open_process(self, command, **kwargs):
        if IS_WINDOWS:
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        proc = await self._real.open_process(command, **kwargs)
        job = current_job.get()
        if job is not None:
            job.add(proc.pid)
        return proc


def patch_sdk() -> None:
    """Makes the Claude Agent SDK start Claude Code with no window, inside the current run's job."""
    from claude_agent_sdk._internal.transport import subprocess_cli

    if not isinstance(subprocess_cli.anyio, _AnyioWithNoWindow):
        subprocess_cli.anyio = _AnyioWithNoWindow(subprocess_cli.anyio)


_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(on: bool) -> None:
    """Asks Windows not to sleep (on) or to sleep as normal again (off). Call from the same thread each time."""
    if IS_WINDOWS:
        _k32.SetThreadExecutionState(_ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0))
