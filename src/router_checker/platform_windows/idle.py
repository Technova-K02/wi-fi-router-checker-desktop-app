"""Time since the last keyboard or mouse input (GetLastInputInfo), so scheduled
Test all only runs while you're away. No admin rights needed."""

from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, wintypes


class LASTINPUTINFO(Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetLastInputInfo.argtypes = [POINTER(LASTINPUTINFO)]
_user32.GetLastInputInfo.restype = wintypes.BOOL
_kernel32 = ctypes.WinDLL("kernel32")
_kernel32.GetTickCount.argtypes = []
_kernel32.GetTickCount.restype = wintypes.DWORD


class WindowsIdleMonitor:
    def idle_seconds(self) -> float:
        info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
        if not _user32.GetLastInputInfo(byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Both are 32-bit millisecond tick counts; the mask handles the 49-day wrap.
        return ((_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000
