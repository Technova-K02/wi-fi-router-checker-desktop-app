"""Route-change notifications (NotifyRouteChange2), used to re-check soon after
Windows switches networks.

The callback runs on a system thread, so it only sets a flag; the app polls
``take_change`` from its own thread.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import byref, c_void_p, wintypes

AF_INET = 2
NO_ERROR = 0

ROUTE_CHANGE_CALLBACK = ctypes.WINFUNCTYPE(None, c_void_p, c_void_p, ctypes.c_int)

_iphlp = ctypes.WinDLL("iphlpapi.dll")
_iphlp.NotifyRouteChange2.argtypes = [
    wintypes.USHORT,
    ROUTE_CHANGE_CALLBACK,
    c_void_p,
    wintypes.BOOLEAN,
    ctypes.POINTER(wintypes.HANDLE),
]
_iphlp.NotifyRouteChange2.restype = wintypes.DWORD
_iphlp.CancelMibChangeNotify2.argtypes = [wintypes.HANDLE]
_iphlp.CancelMibChangeNotify2.restype = wintypes.DWORD


class RouteChangeWatcher:
    """NetworkChangeWatcher for IPv4 route changes."""

    def __init__(self) -> None:
        self._changed = threading.Event()
        self._handle: wintypes.HANDLE | None = None
        self._callback = ROUTE_CHANGE_CALLBACK(self._on_change)  # kept alive while registered
        self._lock = threading.Lock()

    def _on_change(self, _context: int, _row: int, _kind: int) -> None:
        self._changed.set()

    def start(self) -> None:
        with self._lock:
            if self._handle is not None:
                return
            handle = wintypes.HANDLE()
            code = _iphlp.NotifyRouteChange2(AF_INET, self._callback, None, False, byref(handle))
            if code != NO_ERROR:
                raise OSError(code, "NotifyRouteChange2 failed")
            self._handle = handle

    def stop(self) -> None:
        """Unregister; waits for a callback that is running right now."""
        with self._lock:
            if self._handle is None:
                return
            _iphlp.CancelMibChangeNotify2(self._handle)
            self._handle = None
            self._changed.clear()

    def take_change(self) -> bool:
        if self._changed.is_set():
            self._changed.clear()
            return True
        return False
