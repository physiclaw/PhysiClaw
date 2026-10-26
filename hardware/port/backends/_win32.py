"""Win32 job ownership: attach a suspended child before user code can run.

All ctypes declarations stay here. Popen closes its primary thread handle,
so the suspended primary thread is reopened through the documented Toolhelp
API after assigning the process to its job. The job owns descendants before
execution resumes, including children created by Python's venv launcher.
"""

import ctypes
import os
import signal
import subprocess
from ctypes import wintypes
from pathlib import Path
from typing import Any


class _Limits(ctypes.Structure):
    _fields_ = [
        ("ProcessTime", ctypes.c_int64),
        ("JobTime", ctypes.c_int64),
        ("Flags", wintypes.DWORD),
        ("MinWorkingSet", ctypes.c_size_t),
        ("MaxWorkingSet", ctypes.c_size_t),
        ("ActiveProcesses", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("Priority", wintypes.DWORD),
        ("Scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("Basic", _Limits),
        ("IO", ctypes.c_uint64 * 6),
        ("ProcessMemory", ctypes.c_size_t),
        ("JobMemory", ctypes.c_size_t),
        ("PeakProcessMemory", ctypes.c_size_t),
        ("PeakJobMemory", ctypes.c_size_t),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
        ("process_id", wintypes.DWORD),
        ("base_priority", wintypes.LONG),
        ("delta_priority", wintypes.LONG),
        ("flags", wintypes.DWORD),
    ]


def _kernel() -> Any:
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    return kernel


def _resume_primary(kernel: Any, pid: int) -> None:
    snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise _last_error()
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        present = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while present:
            if entry.process_id == pid:
                thread = kernel.OpenThread(
                    0x2, False, entry.thread_id
                )  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise _last_error()
                try:
                    if kernel.ResumeThread(thread) != 1:
                        raise OSError("Could not resume the suspended child thread")
                finally:
                    kernel.CloseHandle(thread)
                return
            present = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise OSError("Suspended child thread was not found")
    finally:
        kernel.CloseHandle(snapshot)


class ProcessTree:
    """One non-inheritable job handle; closing it reaps the entire owned tree."""

    def __init__(self, process: subprocess.Popen, kernel: Any, handle: int):
        self.process = process
        self._kernel = kernel
        self._handle: int | None = handle

    def stop(self, timeout: float, *, cooperative_cancel: bool = False) -> None:
        if self._handle is None:
            return
        try:
            if self.process.poll() is None:
                marker = os.environ.get("PHYSICLAW_HW_CANCEL_FILE")
                try:
                    # An inherited marker is not proof that a child polls it.
                    # Only known cooperative children can avoid CTRL_BREAK,
                    # which may otherwise terminate their venv launcher early.
                    if not (cooperative_cancel and marker and Path(marker).exists()):
                        self.process.send_signal(getattr(signal, "CTRL_BREAK_EVENT"))
                    self.process.wait(timeout=timeout)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
            self.process.wait()


def own_process(process: subprocess.Popen) -> ProcessTree:
    kernel = _kernel()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise _last_error()
    try:
        limits = _ExtendedLimits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise _last_error()
        if not kernel.AssignProcessToJobObject(job, int(getattr(process, "_handle"))):
            raise _last_error()
        _resume_primary(kernel, process.pid)
    except BaseException:
        kernel.CloseHandle(job)
        raise
    return ProcessTree(process, kernel, job)


def _last_error() -> OSError:
    return getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
