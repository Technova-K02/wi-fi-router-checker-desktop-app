"""Gateway IP (GetAdaptersAddresses) and gateway MAC (GetIpNetTable2, SendARP).

Works without location permission and without admin rights.
"""

from __future__ import annotations

import ctypes
import socket
from ctypes import POINTER, Structure, Union, byref, c_void_p, wintypes

from router_checker.core.mac import MacAddress
from router_checker.core.models import GatewayInfo
from router_checker.platform_windows.icmp import ipv4_to_ipaddr

AF_UNSPEC = 0
AF_INET = 2
ERROR_SUCCESS = 0
ERROR_BUFFER_OVERFLOW = 111
GAA_FLAG_SKIP_ANYCAST = 0x2
GAA_FLAG_SKIP_MULTICAST = 0x4
GAA_FLAG_SKIP_DNS_SERVER = 0x8
GAA_FLAG_INCLUDE_GATEWAYS = 0x80
IF_TYPE_IEEE80211 = 71
IF_OPER_STATUS_UP = 1
NL_NEIGHBOR_STATE_INCOMPLETE = 1


class SOCKADDR(Structure):
    _fields_ = [("sa_family", wintypes.USHORT), ("sa_data", ctypes.c_char * 14)]


class SOCKET_ADDRESS(Structure):
    _fields_ = [("lpSockaddr", POINTER(SOCKADDR)), ("iSockaddrLength", ctypes.c_int)]


class _LengthIndex(Structure):
    _fields_ = [("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD)]


class _AlignUnion(Union):
    _fields_ = [("Alignment", ctypes.c_ulonglong), ("s", _LengthIndex)]


class IP_ADAPTER_UNICAST_ADDRESS(Structure):
    pass


IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("u", _AlignUnion),
    ("Next", POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
    # remaining fields not needed
]


class IP_ADAPTER_GATEWAY_ADDRESS(Structure):
    pass


IP_ADAPTER_GATEWAY_ADDRESS._fields_ = [
    ("u", _AlignUnion),
    ("Next", POINTER(IP_ADAPTER_GATEWAY_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
]


class IP_ADAPTER_ADDRESSES(Structure):
    pass


# Only the leading fields we read. We never allocate this struct ourselves, we
# only read it through pointers into the buffer Windows filled.
IP_ADAPTER_ADDRESSES._fields_ = [
    ("u", _AlignUnion),
    ("Next", POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", c_void_p),
    ("FirstMulticastAddress", c_void_p),
    ("FirstDnsServerAddress", c_void_p),
    ("DnsSuffix", wintypes.LPWSTR),
    ("Description", wintypes.LPWSTR),
    ("FriendlyName", wintypes.LPWSTR),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG),
    ("Mtu", wintypes.ULONG),
    ("IfType", wintypes.ULONG),
    ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", wintypes.DWORD),
    ("ZoneIndices", wintypes.ULONG * 16),
    ("FirstPrefix", c_void_p),
    ("TransmitLinkSpeed", ctypes.c_ulonglong),
    ("ReceiveLinkSpeed", ctypes.c_ulonglong),
    ("FirstWinsServerAddress", c_void_p),
    ("FirstGatewayAddress", POINTER(IP_ADAPTER_GATEWAY_ADDRESS)),
    ("Ipv4Metric", wintypes.ULONG),
    ("Ipv6Metric", wintypes.ULONG),
    ("Luid", ctypes.c_ulonglong),
]


class MIB_IPNET_ROW2(Structure):
    _fields_ = [
        ("Address", ctypes.c_ubyte * 28),  # SOCKADDR_INET
        ("InterfaceIndex", wintypes.ULONG),
        ("InterfaceLuid", ctypes.c_ulonglong),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("State", ctypes.c_int),
        ("Flags", ctypes.c_ubyte),
        ("ReachabilityTime", wintypes.ULONG),
    ]


class MIB_IPNET_TABLE2(Structure):
    _fields_ = [("NumEntries", wintypes.ULONG), ("Table", MIB_IPNET_ROW2 * 1)]


_iphlp = ctypes.WinDLL("iphlpapi.dll")
_iphlp.GetAdaptersAddresses.argtypes = [
    wintypes.ULONG,
    wintypes.ULONG,
    c_void_p,
    c_void_p,
    POINTER(wintypes.ULONG),
]
_iphlp.GetAdaptersAddresses.restype = wintypes.ULONG
_iphlp.GetIpNetTable2.argtypes = [wintypes.USHORT, POINTER(POINTER(MIB_IPNET_TABLE2))]
_iphlp.GetIpNetTable2.restype = wintypes.DWORD
_iphlp.FreeMibTable.argtypes = [c_void_p]
_iphlp.FreeMibTable.restype = None
_iphlp.SendARP.argtypes = [wintypes.ULONG, wintypes.ULONG, c_void_p, POINTER(wintypes.ULONG)]
_iphlp.SendARP.restype = wintypes.DWORD


def _ipv4(sock: SOCKET_ADDRESS) -> str | None:
    if not sock.lpSockaddr or sock.lpSockaddr.contents.sa_family != AF_INET:
        return None
    raw = ctypes.string_at(ctypes.addressof(sock.lpSockaddr.contents), 8)
    return socket.inet_ntoa(raw[4:8])


def _adapters() -> list[tuple[str, str, str | None, str | None, int]]:
    """(adapter GUID, friendly name, first IPv4, first IPv4 gateway, IfIndex) of
    Wi-Fi adapters that are up."""
    flags = (
        GAA_FLAG_INCLUDE_GATEWAYS
        | GAA_FLAG_SKIP_ANYCAST
        | GAA_FLAG_SKIP_MULTICAST
        | GAA_FLAG_SKIP_DNS_SERVER
    )
    size = wintypes.ULONG(16 * 1024)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        code = _iphlp.GetAdaptersAddresses(AF_INET, flags, None, buf, byref(size))
        if code != ERROR_BUFFER_OVERFLOW:
            break
    if code != ERROR_SUCCESS:
        raise OSError(code, "GetAdaptersAddresses failed")

    result = []
    ptr = ctypes.cast(buf, POINTER(IP_ADAPTER_ADDRESSES))
    while ptr:
        a = ptr.contents
        if a.IfType == IF_TYPE_IEEE80211 and a.OperStatus == IF_OPER_STATUS_UP:
            local = _ipv4(a.FirstUnicastAddress.contents.Address) if a.FirstUnicastAddress else None
            gateway = None
            gw = a.FirstGatewayAddress
            while gw and gateway is None:
                gateway = _ipv4(gw.contents.Address)
                gw = gw.contents.Next
            name = (a.AdapterName or b"").decode("ascii", errors="replace")
            result.append((name.upper(), a.FriendlyName or "", local, gateway, a.u.s.IfIndex))
        ptr = a.Next
    return result


def neighbor_mac(ip: str, if_index: int | None = None) -> MacAddress | None:
    """MAC for ``ip`` from the IPv4 neighbor (ARP) table, or None."""
    table = POINTER(MIB_IPNET_TABLE2)()
    if _iphlp.GetIpNetTable2(AF_INET, byref(table)) != ERROR_SUCCESS:
        return None
    try:
        target = socket.inet_aton(ip)
        rows = ctypes.cast(ctypes.addressof(table.contents.Table), POINTER(MIB_IPNET_ROW2))
        for i in range(table.contents.NumEntries):
            row = rows[i]
            if bytes(row.Address[4:8]) != target:
                continue
            if if_index is not None and row.InterfaceIndex != if_index:
                continue
            if row.PhysicalAddressLength != 6 or row.State <= NL_NEIGHBOR_STATE_INCOMPLETE:
                continue
            mac = MacAddress.from_bytes(bytes(row.PhysicalAddress[:6]))
            if not mac.is_zero:
                return mac
        return None
    finally:
        _iphlp.FreeMibTable(table)


def arp_resolve(ip: str) -> MacAddress | None:
    """Send an ARP request (SendARP) when the neighbor table has no entry yet."""
    mac_buf = (ctypes.c_ubyte * 8)()
    length = wintypes.ULONG(8)
    if _iphlp.SendARP(ipv4_to_ipaddr(ip), 0, mac_buf, byref(length)) != ERROR_SUCCESS:
        return None
    if length.value != 6:
        return None
    mac = MacAddress.from_bytes(bytes(mac_buf[:6]))
    return None if mac.is_zero else mac


class WindowsNetworkInfoService:
    def __init__(self, interface_id: str | None = None) -> None:
        """``interface_id`` picks a specific Wi-Fi adapter (GUID with braces)."""
        self._interface_id = interface_id.upper() if interface_id else None

    def wifi_gateway(self) -> GatewayInfo | None:
        for guid, name, local, gateway, if_index in _adapters():
            if self._interface_id and guid != self._interface_id:
                continue
            if gateway is None:
                continue
            mac = neighbor_mac(gateway, if_index) or arp_resolve(gateway)
            return GatewayInfo(guid, name, local, gateway, mac)
        return None
