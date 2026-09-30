"""Ping through the Windows ICMP API (IcmpSendEcho2Ex), no admin rights needed.

A source address sends the echoes out of that address's adapter, so the checked
Wi-Fi or Ethernet connection carries them even when a cable or a VPN would
otherwise take the route. IPv4 only for now; domain targets are resolved to
IPv4 addresses.
"""

from __future__ import annotations

import ctypes
import socket
import threading
from ctypes import POINTER, Structure, c_void_p, wintypes

IP_SUCCESS = 0
IP_TTL_EXPIRED_TRANSIT = 11013  # a router on the way answered
INVALID_HANDLE_VALUE = c_void_p(-1).value
PAYLOAD = b"RouterChecker-ping-payload-32byt"  # 32 bytes, like Windows ping


class IP_OPTION_INFORMATION(Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", POINTER(ctypes.c_ubyte)),
    ]


class ICMP_ECHO_REPLY(Structure):
    _fields_ = [
        ("Address", wintypes.ULONG),
        ("Status", wintypes.ULONG),
        ("RoundTripTime", wintypes.ULONG),  # ms
        ("DataSize", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("Data", c_void_p),
        ("Options", IP_OPTION_INFORMATION),
    ]


_iphlp = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)
_iphlp.IcmpCreateFile.argtypes = []
_iphlp.IcmpCreateFile.restype = wintypes.HANDLE
_iphlp.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
_iphlp.IcmpCloseHandle.restype = wintypes.BOOL
_iphlp.IcmpSendEcho2Ex.argtypes = [
    wintypes.HANDLE, wintypes.HANDLE, c_void_p, c_void_p, wintypes.ULONG, wintypes.ULONG,
    c_void_p, wintypes.WORD, POINTER(IP_OPTION_INFORMATION), c_void_p, wintypes.DWORD,
    wintypes.DWORD,
]  # fmt: skip
_iphlp.IcmpSendEcho2Ex.restype = wintypes.DWORD


def ipv4_to_ipaddr(address: str) -> int:
    """IPAddr is the address in network byte order, read as a little-endian ULONG."""
    return int.from_bytes(socket.inet_aton(address), "little")


def ipaddr_to_ipv4(value: int) -> str:
    return socket.inet_ntoa(value.to_bytes(4, "little"))


class WindowsPingService:
    def ping(
        self,
        address: str,
        count: int,
        timeout_ms: int,
        spacing_ms: int,
        stop: threading.Event | None = None,
        source: str | None = None,
    ) -> list[float | None]:
        dest = ipv4_to_ipaddr(address)
        src = ipv4_to_ipaddr(source) if source else 0  # 0: wherever Windows routes it
        if stop is None:
            stop = threading.Event()  # never set
        handle = _iphlp.IcmpCreateFile()
        if not handle or handle == INVALID_HANDLE_VALUE:
            raise OSError(ctypes.get_last_error(), "IcmpCreateFile failed")
        reply_size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(PAYLOAD) + 8 + 64
        reply = ctypes.create_string_buffer(reply_size)
        payload = ctypes.create_string_buffer(PAYLOAD, len(PAYLOAD))
        results: list[float | None] = []
        try:
            for i in range(count):
                # The pause between echoes doubles as the check for "stop now".
                if stop.wait(spacing_ms / 1000 if i else 0):
                    break
                n = _iphlp.IcmpSendEcho2Ex(
                    handle, None, None, None, src, dest, payload, len(PAYLOAD), None,
                    reply, reply_size, timeout_ms,
                )  # fmt: skip
                if n == 0:
                    results.append(None)  # timed out or unreachable
                    continue
                echo = ICMP_ECHO_REPLY.from_buffer(reply)
                ok = echo.Status == IP_SUCCESS and echo.Address == dest
                results.append(float(echo.RoundTripTime) if ok else None)
        finally:
            _iphlp.IcmpCloseHandle(handle)
        return results

    def hop(self, address: str, ttl: int, timeout_ms: int, source: str | None = None) -> str | None:
        """One echo toward ``address`` that may only travel ``ttl`` hops: the router
        where it runs out answers "time exceeded" (or ``address`` itself if it's that
        close). Its address, or None if nothing answered."""
        handle = _iphlp.IcmpCreateFile()
        if not handle or handle == INVALID_HANDLE_VALUE:
            raise OSError(ctypes.get_last_error(), "IcmpCreateFile failed")
        reply_size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(PAYLOAD) + 8 + 64
        reply = ctypes.create_string_buffer(reply_size)
        payload = ctypes.create_string_buffer(PAYLOAD, len(PAYLOAD))
        options = IP_OPTION_INFORMATION(Ttl=ttl)
        src = ipv4_to_ipaddr(source) if source else 0
        try:
            n = _iphlp.IcmpSendEcho2Ex(
                handle, None, None, None, src, ipv4_to_ipaddr(address), payload,
                len(PAYLOAD), ctypes.byref(options), reply, reply_size, timeout_ms,
            )  # fmt: skip
        finally:
            _iphlp.IcmpCloseHandle(handle)
        if n == 0:
            return None
        echo = ICMP_ECHO_REPLY.from_buffer(reply)
        if echo.Status not in (IP_SUCCESS, IP_TTL_EXPIRED_TRANSIT):
            return None
        return ipaddr_to_ipv4(echo.Address)
