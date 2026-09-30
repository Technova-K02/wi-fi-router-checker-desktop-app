"""Adapters and gateway IP (GetAdaptersAddresses), which adapters are physical
(GetIfTable2), which one carries internet traffic (GetBestInterfaceEx) and the
gateway MAC (GetIpNetTable2, SendARP). ``core.links`` picks the one to check.

Works without location permission and without admin rights.
"""

from __future__ import annotations

import ctypes
import socket
from ctypes import POINTER, Structure, Union, byref, c_void_p, wintypes

from router_checker.core.links import Adapter, choose_link
from router_checker.core.mac import MacAddress
from router_checker.core.models import GatewayInfo, LinkChoice, LinkKind
from router_checker.platform_windows.icmp import ipv4_to_ipaddr

AF_UNSPEC = 0
AF_INET = 2
ERROR_SUCCESS = 0
ERROR_BUFFER_OVERFLOW = 111
GAA_FLAG_SKIP_ANYCAST = 0x2
GAA_FLAG_SKIP_MULTICAST = 0x4
GAA_FLAG_SKIP_DNS_SERVER = 0x8
GAA_FLAG_INCLUDE_GATEWAYS = 0x80
IF_TYPE_ETHERNET_CSMACD = 6
IF_TYPE_IEEE80211 = 71
IF_HARDWARE_INTERFACE = 0x1  # MIB_IF_ROW2.InterfaceAndOperStatusFlags bits
IF_FILTER_INTERFACE = 0x2
INTERNET_PROBE = "1.1.1.1"  # only a routing-table lookup: nothing is sent
_KINDS = {IF_TYPE_IEEE80211: LinkKind.WIFI, IF_TYPE_ETHERNET_CSMACD: LinkKind.ETHERNET}
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


class GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class MIB_IF_ROW2(Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_ulonglong),
        ("InterfaceIndex", wintypes.ULONG),
        ("InterfaceGuid", GUID),
        ("Alias", ctypes.c_wchar * 257),
        ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", wintypes.ULONG),
        ("Type", wintypes.ULONG),
        ("TunnelType", ctypes.c_int),
        ("MediaType", ctypes.c_int),
        ("PhysicalMediumType", ctypes.c_int),
        ("AccessType", ctypes.c_int),
        ("DirectionType", ctypes.c_int),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_int),
        ("AdminStatus", ctypes.c_int),
        ("MediaConnectState", ctypes.c_int),
        ("NetworkGuid", GUID),
        ("ConnectionType", ctypes.c_int),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("Counters", ctypes.c_ulonglong * 18),  # In/Out octets, packets, errors… OutQLen
    ]


class MIB_IF_TABLE2(Structure):
    _fields_ = [("NumEntries", wintypes.ULONG), ("Table", MIB_IF_ROW2 * 1)]


class SOCKADDR_IN(Structure):
    _fields_ = [
        ("sin_family", ctypes.c_short),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_char * 8),
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
_iphlp.GetIfTable2.argtypes = [POINTER(POINTER(MIB_IF_TABLE2))]
_iphlp.GetIfTable2.restype = wintypes.DWORD
_iphlp.GetBestInterfaceEx.argtypes = [POINTER(SOCKADDR_IN), POINTER(wintypes.DWORD)]
_iphlp.GetBestInterfaceEx.restype = wintypes.DWORD
assert ctypes.sizeof(MIB_IF_ROW2) == 1352, "MIB_IF_ROW2 layout"


def _ipv4(sock: SOCKET_ADDRESS) -> str | None:
    if not sock.lpSockaddr or sock.lpSockaddr.contents.sa_family != AF_INET:
        return None
    raw = ctypes.string_at(ctypes.addressof(sock.lpSockaddr.contents), 8)
    return socket.inet_ntoa(raw[4:8])


def _hardware_indexes() -> set[int]:
    """Indexes of physical network cards (not Hyper-V, VMware, VPN or other virtual
    adapters, and not the filter layers Windows stacks on top of cards)."""
    table = POINTER(MIB_IF_TABLE2)()
    code = _iphlp.GetIfTable2(byref(table))
    if code != ERROR_SUCCESS:
        raise OSError(code, "GetIfTable2 failed")
    try:
        rows = ctypes.cast(ctypes.addressof(table.contents.Table), POINTER(MIB_IF_ROW2))
        found = set()
        for i in range(table.contents.NumEntries):
            flags = rows[i].InterfaceAndOperStatusFlags
            if flags & IF_HARDWARE_INTERFACE and not flags & IF_FILTER_INTERFACE:
                found.add(rows[i].InterfaceIndex)
        return found
    finally:
        _iphlp.FreeMibTable(table)


def internet_index() -> int | None:
    """The adapter Windows would send internet traffic through (a VPN, when one is
    on), or None if there's no route to the internet."""
    dest = SOCKADDR_IN(AF_INET, 0, (ctypes.c_ubyte * 4)(*socket.inet_aton(INTERNET_PROBE)))
    index = wintypes.DWORD()
    if _iphlp.GetBestInterfaceEx(byref(dest), byref(index)) != ERROR_SUCCESS:
        return None
    return index.value


def adapters() -> list[Adapter]:
    """Every adapter with IPv4, as ``core.links`` needs them."""
    hardware = _hardware_indexes()
    return [
        Adapter(guid, name, index, _KINDS.get(if_type), index in hardware, up, local, gateway)
        for guid, name, local, gateway, index, if_type, up in _adapter_rows()
    ]


def _adapter_rows() -> list[tuple[str, str, str | None, str | None, int, int, bool]]:
    """(GUID, friendly name, first IPv4, first IPv4 gateway, IfIndex, IfType, up)."""
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
        local = _ipv4(a.FirstUnicastAddress.contents.Address) if a.FirstUnicastAddress else None
        gateway = None
        gw = a.FirstGatewayAddress
        while gw and gateway is None:
            gateway = _ipv4(gw.contents.Address)
            gw = gw.contents.Next
        name = (a.AdapterName or b"").decode("ascii", errors="replace")
        up = a.OperStatus == IF_OPER_STATUS_UP
        result.append(
            (name.upper(), a.FriendlyName or "", local, gateway, a.u.s.IfIndex, a.IfType, up)
        )
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

    def gateway(self, choice: LinkChoice = LinkChoice.AUTO) -> GatewayInfo | None:
        chosen = choose_link(adapters(), internet_index(), choice)
        if chosen.adapter is None:
            return None
        return _gateway_info(chosen.adapter, chosen.vpn)

    def wifi_gateway(self) -> GatewayInfo | None:
        """The Wi-Fi adapter's gateway, whatever carries the internet (Test all)."""
        for adapter in adapters():
            if adapter.kind is not LinkKind.WIFI or not adapter.usable:
                continue
            if self._interface_id and adapter.interface_id != self._interface_id:
                continue
            return _gateway_info(adapter, vpn=False)
        return None


def _gateway_info(adapter: Adapter, vpn: bool) -> GatewayInfo:
    gateway = adapter.gateway_ip
    assert gateway is not None and adapter.kind is not None  # usable adapters have them
    mac = neighbor_mac(gateway, adapter.index) or arp_resolve(gateway)
    return GatewayInfo(
        adapter.interface_id, adapter.name, adapter.local_ip, gateway, mac, adapter.kind, vpn
    )
