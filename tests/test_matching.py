from fakes import entry, gateway_info, mac
from router_checker.core.matching import MatchMethod, identify_current, router_state
from router_checker.core.models import Router, RouterState, WifiConnection

ZTE_LAN = "B0-0A-D5-9A-7B-B4"
ZTE_BSSID = "B0-0A-D5-9A-7B-B8"

zte = Router("zte", "ZTE", "#0078D4", ssid="ZTE-Home", macs=(mac(ZTE_LAN),))
other = Router("oth", "Other", "#107C10", ssid="Other", macs=(mac("11-22-33-44-55-66"),))
routers = [zte, other]


def conn(ssid="ZTE-Home", bssid=ZTE_BSSID):
    return WifiConnection(ssid, mac(bssid) if bssid else None, 90)


def test_match_by_bssid() -> None:
    r = Router("x", "X", "#000000", macs=(mac(ZTE_BSSID),))
    m = identify_current([r], conn(ssid="Whatever"), gateway_info(gw_mac=ZTE_LAN))
    assert m.router == r and m.method is MatchMethod.BSSID
    assert m.macs_to_link == (mac(ZTE_LAN),)


def test_match_by_ssid_links_gateway_and_bssid() -> None:
    m = identify_current(routers, conn(), gateway_info(gw_mac="B0-0A-D5-9A-7B-00"))
    assert m.router == zte and m.method is MatchMethod.SSID
    assert m.macs_to_link == (mac("B0-0A-D5-9A-7B-00"), mac(ZTE_BSSID))


def test_nothing_to_link_when_known() -> None:
    m = identify_current(routers, conn(bssid=None), gateway_info(gw_mac=ZTE_LAN))
    assert m.router == zte and m.macs_to_link == ()


def test_no_location_falls_back_to_gateway_mac() -> None:
    m = identify_current(routers, None, gateway_info(gw_mac=ZTE_LAN))
    assert m.router == zte and m.method is MatchMethod.GATEWAY_MAC
    assert m.macs_to_link == ()


def test_gateway_match_learns_connected_bssid() -> None:
    m = identify_current(
        [Router("z", "Z", "#000000", macs=(mac(ZTE_LAN),))],
        conn(ssid="X"),
        gateway_info(gw_mac=ZTE_LAN),
    )
    assert m.method is MatchMethod.GATEWAY_MAC
    assert m.macs_to_link == (mac(ZTE_BSSID),)


def test_unknown_network() -> None:
    m = identify_current(
        routers,
        conn(ssid="Cafe", bssid="99-99-99-99-99-99"),
        gateway_info(gw_mac="99-99-99-99-99-01"),
    )
    assert m.router is None and m.macs_to_link == ()
    assert identify_current(routers, None, None).router is None


def test_does_not_steal_mac_owned_by_another_router() -> None:
    m = identify_current(routers, conn(bssid=None), gateway_info(gw_mac="11-22-33-44-55-66"))
    assert m.router == zte
    assert m.macs_to_link == ()


def test_shared_ssid_disambiguated_by_gateway_mac() -> None:
    a = Router("a", "A", "#000000", ssid="Mesh", macs=(mac("AA-AA-AA-AA-AA-AA"),))
    b = Router("b", "B", "#000000", ssid="Mesh", macs=(mac("BB-BB-BB-BB-BB-BB"),))
    m = identify_current(
        [a, b], conn(ssid="Mesh", bssid=None), gateway_info(gw_mac="BB-BB-BB-BB-BB-BB")
    )
    assert m.router == b
    m = identify_current([a, b], conn(ssid="Mesh", bssid=None), gateway_info(gw_mac=None))
    assert m.router is None


def test_router_states() -> None:
    scan = [entry(ZTE_BSSID, ssid="ZTE-Home")]
    assert router_state(zte, zte, True, scan) is RouterState.ONLINE
    assert router_state(zte, zte, False, None) is RouterState.VISIBLE
    assert router_state(other, zte, True, scan) is RouterState.NOT_FOUND
    assert router_state(other, zte, True, None) is RouterState.UNKNOWN
    assert router_state(zte, None, False, scan) is RouterState.VISIBLE
