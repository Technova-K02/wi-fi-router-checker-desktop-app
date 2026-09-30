"""Wait for processes to end (OpenProcess + WaitForMultipleObjects), so
``--exit`` returns only once the running copy no longer holds the .exe."""

from __future__ import annotations

import ctypes
from collections.abc import Sequence
from ctypes import wintypes

SYNCHRONIZE = 0x00100000

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.BOOL,
    wintypes.DWORD,
]
_kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def wait_for_exit(pids: Sequence[int], timeout_s: float) -> bool:
    """True once all these processes have ended (ones already gone count as ended)."""
    handles = [h for h in (_kernel32.OpenProcess(SYNCHRONIZE, False, pid) for pid in pids) if h]
    if not handles:
        return True
    try:
        array = (wintypes.HANDLE * len(handles))(*handles)  # at most 64; we pass 1 or 2
        result = _kernel32.WaitForMultipleObjects(len(array), array, True, round(timeout_s * 1000))
        return result < len(array)  # WAIT_OBJECT_0; not WAIT_TIMEOUT or WAIT_FAILED
    finally:
        for handle in handles:
            _kernel32.CloseHandle(handle)
