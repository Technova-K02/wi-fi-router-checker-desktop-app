"""Native Wifi API (wlanapi.dll) through ctypes.

On Windows 11 24H2+, WlanScan, WlanGetNetworkBssList, WlanGetAvailableNetworkList
and WlanQueryInterface(current_connection) fail with ERROR_ACCESS_DENIED unless
location access for desktop apps is allowed. That becomes LocationPermissionError.
"""

from __future__ import annotations

import ctypes
import threading
import time
import uuid
from ctypes import POINTER, Structure, byref, c_void_p, wintypes

from router_checker.core.errors import LocationPermissionError, WifiUnavailableError
from router_checker.core.mac import MacAddress
from router_checker.core.models import ScanEntry, WifiConnection
from router_checker.core.wifi_info import (
    band_from_frequency,
    channel_from_frequency,
    parse_bss_load,
)

ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_NOT_FOUND = 1168
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_INVALID_STATE = 5023
ERROR_NDIS_DOT11_POWER_STATE_INVALID = 0x80342002

WLAN_CLIENT_VERSION_2 = 2
WLAN_MAX_PHY_TYPE_NUMBER = 8
DOT11_BSS_TYPE_INFRASTRUCTURE = 1
DOT11_BSS_TYPE_ANY = 3
WLAN_INTF_OPCODE_CURRENT_CONNECTION = 7
WLAN_INTERFACE_STATE_CONNECTED = 1
WLAN_CONNECTION_MODE_PROFILE = 0

WLAN_NOTIFICATION_SOURCE_NONE = 0
WLAN_NOTIFICATION_SOURCE_ACM = 0x08
WLAN_NOTIFICATION_ACM_SCAN_COMPLETE = 7
WLAN_NOTIFICATION_ACM_SCAN_FAIL = 8

SCAN_WAIT_S = 4.0
SCAN_POLL_S = 0.1  # how often a scan wait looks at its stop flag


class GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]

    def __str__(self) -> str:
        return "{" + str(uuid.UUID(bytes_le=bytes(self))).upper() + "}"


class DOT11_SSID(Structure):
    _fields_ = [("uSSIDLength", wintypes.ULONG), ("ucSSID", ctypes.c_ubyte * 32)]

    def text(self) -> str:
        raw = bytes(self.ucSSID[: min(self.uSSIDLength, 32)])
        return raw.decode("utf-8", errors="replace")


class WLAN_INTERFACE_INFO(Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", wintypes.WCHAR * 256),
        ("isState", ctypes.c_int),
    ]


class WLAN_INTERFACE_INFO_LIST(Structure):
    _fields_ = [
        ("dwNumberOfItems", wintypes.DWORD),
        ("dwIndex", wintypes.DWORD),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
    ]


class WLAN_RATE_SET(Structure):
    _fields_ = [("uRateSetLength", wintypes.ULONG), ("usRateSet", wintypes.USHORT * 126)]


class WLAN_BSS_ENTRY(Structure):
    _fields_ = [
        ("dot11Ssid", DOT11_SSID),
        ("uPhyId", wintypes.ULONG),
        ("dot11Bssid", ctypes.c_ubyte * 6),
        ("dot11BssType", ctypes.c_int),
        ("dot11BssPhyType", ctypes.c_int),
        ("lRssi", wintypes.LONG),
        ("uLinkQuality", wintypes.ULONG),
        ("bInRegDomain", wintypes.BOOLEAN),
        ("usBeaconPeriod", wintypes.USHORT),
        ("ullTimestamp", ctypes.c_ulonglong),
        ("ullHostTimestamp", ctypes.c_ulonglong),
        ("usCapabilityInformation", wintypes.USHORT),
        ("ulChCenterFrequency", wintypes.ULONG),  # kHz
        ("wlanRateSet", WLAN_RATE_SET),
        ("ulIeOffset", wintypes.ULONG),  # from the start of this entry
        ("ulIeSize", wintypes.ULONG),
    ]


class WLAN_BSS_LIST(Structure):
    _fields_ = [
        ("dwTotalSize", wintypes.DWORD),
        ("dwNumberOfItems", wintypes.DWORD),
        ("wlanBssEntries", WLAN_BSS_ENTRY * 1),
    ]


class WLAN_ASSOCIATION_ATTRIBUTES(Structure):
    _fields_ = [
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", ctypes.c_int),
        ("dot11Bssid", ctypes.c_ubyte * 6),
        ("dot11PhyType", ctypes.c_int),
        ("uDot11PhyIndex", wintypes.ULONG),
        ("wlanSignalQuality", wintypes.ULONG),
        ("ulRxRate", wintypes.ULONG),
        ("ulTxRate", wintypes.ULONG),
    ]


class WLAN_SECURITY_ATTRIBUTES(Structure):
    _fields_ = [
        ("bSecurityEnabled", wintypes.BOOL),
        ("bOneXEnabled", wintypes.BOOL),
        ("dot11AuthAlgorithm", ctypes.c_int),
        ("dot11CipherAlgorithm", ctypes.c_int),
    ]


class WLAN_CONNECTION_ATTRIBUTES(Structure):
    _fields_ = [
        ("isState", ctypes.c_int),
        ("wlanConnectionMode", ctypes.c_int),
        ("strProfileName", wintypes.WCHAR * 256),
        ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
        ("wlanSecurityAttributes", WLAN_SECURITY_ATTRIBUTES),
    ]


class WLAN_PROFILE_INFO(Structure):
    _fields_ = [("strProfileName", wintypes.WCHAR * 256), ("dwFlags", wintypes.DWORD)]


class WLAN_PROFILE_INFO_LIST(Structure):
    _fields_ = [
        ("dwNumberOfItems", wintypes.DWORD),
        ("dwIndex", wintypes.DWORD),
        ("ProfileInfo", WLAN_PROFILE_INFO * 1),
    ]


class WLAN_CONNECTION_PARAMETERS(Structure):
    _fields_ = [
        ("wlanConnectionMode", ctypes.c_int),
        ("strProfile", wintypes.LPCWSTR),
        ("pDot11Ssid", POINTER(DOT11_SSID)),
        ("pDesiredBssidList", c_void_p),
        ("dot11BssType", ctypes.c_int),
        ("dwFlags", wintypes.DWORD),
    ]


class WLAN_NOTIFICATION_DATA(Structure):
    _fields_ = [
        ("NotificationSource", wintypes.DWORD),
        ("NotificationCode", wintypes.DWORD),
        ("InterfaceGuid", GUID),
        ("dwDataSize", wintypes.DWORD),
        ("pData", c_void_p),
    ]


WLAN_NOTIFICATION_CALLBACK = ctypes.WINFUNCTYPE(None, POINTER(WLAN_NOTIFICATION_DATA), c_void_p)

_wlan = ctypes.WinDLL("wlanapi.dll")

_wlan.WlanOpenHandle.argtypes = [
    wintypes.DWORD,
    c_void_p,
    POINTER(wintypes.DWORD),
    POINTER(wintypes.HANDLE),
]
_wlan.WlanOpenHandle.restype = wintypes.DWORD
_wlan.WlanCloseHandle.argtypes = [wintypes.HANDLE, c_void_p]
_wlan.WlanCloseHandle.restype = wintypes.DWORD
_wlan.WlanFreeMemory.argtypes = [c_void_p]
_wlan.WlanFreeMemory.restype = None
_wlan.WlanEnumInterfaces.argtypes = [
    wintypes.HANDLE,
    c_void_p,
    POINTER(POINTER(WLAN_INTERFACE_INFO_LIST)),
]
_wlan.WlanEnumInterfaces.restype = wintypes.DWORD
_wlan.WlanScan.argtypes = [wintypes.HANDLE, POINTER(GUID), POINTER(DOT11_SSID), c_void_p, c_void_p]
_wlan.WlanScan.restype = wintypes.DWORD
_wlan.WlanGetNetworkBssList.argtypes = [
    wintypes.HANDLE, POINTER(GUID), POINTER(DOT11_SSID), ctypes.c_int, wintypes.BOOL,
    c_void_p, POINTER(POINTER(WLAN_BSS_LIST)),
]  # fmt: skip
_wlan.WlanGetNetworkBssList.restype = wintypes.DWORD
_wlan.WlanQueryInterface.argtypes = [
    wintypes.HANDLE, POINTER(GUID), ctypes.c_int, c_void_p, POINTER(wintypes.DWORD),
    POINTER(c_void_p), POINTER(ctypes.c_int),
]  # fmt: skip
_wlan.WlanQueryInterface.restype = wintypes.DWORD
_wlan.WlanGetProfileList.argtypes = [
    wintypes.HANDLE,
    POINTER(GUID),
    c_void_p,
    POINTER(POINTER(WLAN_PROFILE_INFO_LIST)),
]
_wlan.WlanGetProfileList.restype = wintypes.DWORD
_wlan.WlanConnect.argtypes = [
    wintypes.HANDLE,
    POINTER(GUID),
    POINTER(WLAN_CONNECTION_PARAMETERS),
    c_void_p,
]
_wlan.WlanConnect.restype = wintypes.DWORD
_wlan.WlanRegisterNotification.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, WLAN_NOTIFICATION_CALLBACK, c_void_p,
    c_void_p, POINTER(wintypes.DWORD),
]  # fmt: skip
_wlan.WlanRegisterNotification.restype = wintypes.DWORD


def _check(code: int, what: str) -> None:
    if code == ERROR_SUCCESS:
        return
    if code == ERROR_ACCESS_DENIED:
        raise LocationPermissionError(
            f"{what}: access denied. Turn on 'Let desktop apps access your location' "
            "in Settings > Privacy & security > Location."
        )
    if code == ERROR_SERVICE_NOT_ACTIVE:
        raise WifiUnavailableError("the WLAN AutoConfig service is not running")
    if code == ERROR_NDIS_DOT11_POWER_STATE_INVALID:
        raise WifiUnavailableError("the Wi-Fi radio is turned off")
    raise WifiUnavailableError(f"{what} failed with Windows error {code}")


class WindowsWifiService:
    """WifiService and RouterSwitcher on the first Wi-Fi adapter.

    A single client handle is opened lazily and reused; calls are serialized.
    """

    def __init__(self) -> None:
        self._handle: wintypes.HANDLE | None = None
        self._lock = threading.RLock()
        self._callback: object = None  # keeps the ctypes callback alive while registered

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                _wlan.WlanCloseHandle(self._handle, None)
                self._handle = None

    def __enter__(self) -> WindowsWifiService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- handle and interface -----------------------------------------------

    def _client(self) -> wintypes.HANDLE:
        if self._handle is None:
            negotiated = wintypes.DWORD()
            handle = wintypes.HANDLE()
            _check(
                _wlan.WlanOpenHandle(WLAN_CLIENT_VERSION_2, None, byref(negotiated), byref(handle)),
                "WlanOpenHandle",
            )
            self._handle = handle
        return self._handle

    def interfaces(self) -> list[tuple[GUID, str, int]]:
        """(GUID, description, state) of every Wi-Fi adapter."""
        with self._lock:
            ptr = POINTER(WLAN_INTERFACE_INFO_LIST)()
            _check(_wlan.WlanEnumInterfaces(self._client(), None, byref(ptr)), "WlanEnumInterfaces")
            try:
                count = ptr.contents.dwNumberOfItems
                items = ctypes.cast(
                    ctypes.addressof(ptr.contents.InterfaceInfo), POINTER(WLAN_INTERFACE_INFO)
                )
                result = []
                for i in range(count):
                    guid = GUID.from_buffer_copy(items[i].InterfaceGuid)
                    result.append((guid, items[i].strInterfaceDescription, items[i].isState))
                return result
            finally:
                _wlan.WlanFreeMemory(ptr)

    def _interface(self) -> GUID:
        found = self.interfaces()
        if not found:
            raise WifiUnavailableError("no Wi-Fi adapter found")
        connected = [g for g, _, state in found if state == WLAN_INTERFACE_STATE_CONNECTED]
        return (connected or [found[0][0]])[0]

    def interface_id(self) -> str:
        return str(self._interface())

    # --- WifiService ----------------------------------------------------------

    def current_connection(self) -> WifiConnection | None:
        with self._lock:
            guid = self._interface()
            size = wintypes.DWORD()
            data = c_void_p()
            value_type = ctypes.c_int()
            code = _wlan.WlanQueryInterface(
                self._client(), byref(guid), WLAN_INTF_OPCODE_CURRENT_CONNECTION, None,
                byref(size), byref(data), byref(value_type),
            )  # fmt: skip
            if code in (ERROR_INVALID_STATE, ERROR_NOT_FOUND):
                return None  # not connected
            _check(code, "WlanQueryInterface(current_connection)")
            try:
                attrs = ctypes.cast(data, POINTER(WLAN_CONNECTION_ATTRIBUTES)).contents
                if attrs.isState != WLAN_INTERFACE_STATE_CONNECTED:
                    return None
                assoc = attrs.wlanAssociationAttributes
                bssid = MacAddress.from_bytes(bytes(assoc.dot11Bssid))
                return WifiConnection(
                    ssid=assoc.dot11Ssid.text(),
                    bssid=None if bssid.is_zero else bssid,
                    signal_quality=int(assoc.wlanSignalQuality),
                    profile_name=attrs.strProfileName,
                    interface_id=str(guid),
                )
            finally:
                _wlan.WlanFreeMemory(data)

    def scan(
        self, stop: threading.Event | None = None, wait_s: float = SCAN_WAIT_S
    ) -> list[ScanEntry]:
        """Request a fresh scan, wait for it to finish (at most ``wait_s``, and no longer
        once ``stop`` is set), then read the BSS list."""
        with self._lock:
            guid = self._interface()
            self._request_scan(guid, wait_s, stop)
            return self._bss_list(guid)

    def _request_scan(self, guid: GUID, wait_s: float, stop: threading.Event | None) -> None:
        done = threading.Event()

        @WLAN_NOTIFICATION_CALLBACK
        def on_notification(data: ctypes._Pointer, _ctx: int) -> None:
            n = data.contents
            if n.NotificationSource == WLAN_NOTIFICATION_SOURCE_ACM and n.NotificationCode in (
                WLAN_NOTIFICATION_ACM_SCAN_COMPLETE,
                WLAN_NOTIFICATION_ACM_SCAN_FAIL,
            ):
                done.set()

        self._callback = on_notification
        client = self._client()
        registered = (
            _wlan.WlanRegisterNotification(
                client, WLAN_NOTIFICATION_SOURCE_ACM, True, on_notification, None, None, None
            )
            == ERROR_SUCCESS
        )
        try:
            _check(_wlan.WlanScan(client, byref(guid), None, None, None), "WlanScan")
            deadline = time.monotonic() + (wait_s if registered else min(wait_s, 2.0))
            while not done.wait(SCAN_POLL_S):
                if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                    break
        finally:
            if registered:
                _wlan.WlanRegisterNotification(
                    client,
                    WLAN_NOTIFICATION_SOURCE_NONE,
                    True,
                    WLAN_NOTIFICATION_CALLBACK(),
                    None,
                    None,
                    None,
                )

    def _bss_list(self, guid: GUID) -> list[ScanEntry]:
        ptr = POINTER(WLAN_BSS_LIST)()
        _check(
            _wlan.WlanGetNetworkBssList(
                self._client(), byref(guid), None, DOT11_BSS_TYPE_ANY, False, None, byref(ptr)
            ),
            "WlanGetNetworkBssList",
        )
        try:
            base = ctypes.addressof(ptr.contents.wlanBssEntries)
            entries = []
            for i in range(ptr.contents.dwNumberOfItems):
                address = base + i * ctypes.sizeof(WLAN_BSS_ENTRY)
                e = WLAN_BSS_ENTRY.from_address(address)
                ies = ctypes.string_at(address + e.ulIeOffset, e.ulIeSize) if e.ulIeSize else b""
                freq = e.ulChCenterFrequency // 1000
                entries.append(
                    ScanEntry(
                        ssid=e.dot11Ssid.text(),
                        bssid=MacAddress.from_bytes(bytes(e.dot11Bssid)),
                        rssi=int(e.lRssi),
                        link_quality=int(e.uLinkQuality),
                        frequency_mhz=freq,
                        channel=channel_from_frequency(freq),
                        band=band_from_frequency(freq),
                        bss_load=parse_bss_load(ies),
                    )
                )
            return entries
        finally:
            _wlan.WlanFreeMemory(ptr)

    def location_allowed(self) -> bool:
        """Probe the permission with the cached BSS list (no new scan)."""
        with self._lock:
            guid = self._interface()
            ptr = POINTER(WLAN_BSS_LIST)()
            code = _wlan.WlanGetNetworkBssList(
                self._client(), byref(guid), None, DOT11_BSS_TYPE_ANY, False, None, byref(ptr)
            )
            if code == ERROR_ACCESS_DENIED:
                return False
            _check(code, "WlanGetNetworkBssList")
            _wlan.WlanFreeMemory(ptr)
            return True

    def saved_profiles(self) -> list[str]:
        with self._lock:
            guid = self._interface()
            ptr = POINTER(WLAN_PROFILE_INFO_LIST)()
            _check(
                _wlan.WlanGetProfileList(self._client(), byref(guid), None, byref(ptr)),
                "WlanGetProfileList",
            )
            try:
                items = ctypes.cast(
                    ctypes.addressof(ptr.contents.ProfileInfo), POINTER(WLAN_PROFILE_INFO)
                )
                return [items[i].strProfileName for i in range(ptr.contents.dwNumberOfItems)]
            finally:
                _wlan.WlanFreeMemory(ptr)

    # --- RouterSwitcher (used from Phase 4) -----------------------------------

    def connect(self, profile_name: str) -> None:
        """Ask Windows to connect with a saved profile; does not wait for an IP."""
        with self._lock:
            guid = self._interface()
            params = WLAN_CONNECTION_PARAMETERS(
                wlanConnectionMode=WLAN_CONNECTION_MODE_PROFILE,
                strProfile=profile_name,
                pDot11Ssid=None,
                pDesiredBssidList=None,
                dot11BssType=DOT11_BSS_TYPE_INFRASTRUCTURE,
                dwFlags=0,
            )
            _check(
                _wlan.WlanConnect(self._client(), byref(guid), byref(params), None), "WlanConnect"
            )
