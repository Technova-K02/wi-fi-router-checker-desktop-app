import pytest

from fakes import cable_gateway, entry, gateway_info, mac
from router_checker.core.middle import (
    Endpoint,
    bssid_order,
    colon_mac,
    could_be_middle,
    gateway_key,
    is_private_ipv4,
    middle_suggestion,
    parse_endpoint,
    router_at,
)
from router_checker.core.models import Router
from router_checker.core.settings import Settings, settings_from_json, settings_to_json

LAN, B24, B5, OTHER = (
    "B0-0A-D5-9A-7B-B4",
    "B0-0A-D5-9A-7B-B6",
    "B0-0A-D5-9A-7B-B8",
    "22-22-22-22-22-22",
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("192.168.8.1:8080", Endpoint("192.168.8.1", 8080)),
        ("  192.168.8.1  ", Endpoint("192.168.8.1", 80)),
        ("http://192.168.8.1:8080/", Endpoint("192.168.8.1", 8080)),
        ("HTTP://middle.lan:81", Endpoint("middle.lan", 81)),
        ("192.168.8.1:0", None),
        ("192.168.8.1:70000", None),
        ("192.168.8.1:port", None),
        ("192.168.8.1/change_router", None),
        ("300.1.1.1", None),
        ("", None),
    ],
)
def test_parse_endpoint(text, expected) -> None:
    assert parse_endpoint(text) == expected


def test_the_change_url_uses_the_mac_with_colons() -> None:
    endpoint = Endpoint("192.168.8.1", 8080)
    assert colon_mac(mac(B5)) == "b0:0a:d5:9a:7b:b8"
    assert endpoint.change_url(mac(B5)) == (
        "http://192.168.8.1:8080/change_router?router=b0:0a:d5:9a:7b:b8"
    )


def test_the_middle_router_is_recognised_as_the_gateway() -> None:
    endpoint = Endpoint("192.168.8.1", 8080)
    assert endpoint.is_gateway(gateway_info("192.168.8.1", LAN))
    assert not endpoint.is_gateway(gateway_info("192.168.1.1", LAN))
    assert not endpoint.is_gateway(None)


def zte(**kw) -> Router:
    return Router("zte", "ZTE", "#0078D4", macs=(mac(LAN), mac(B24), mac(B5)), **kw)


def test_the_wifi_mac_that_worked_last_comes_first() -> None:
    assert bssid_order(zte(middle_bssid=mac(B5)), None) == (mac(B5), mac(LAN), mac(B24))


def test_then_the_strongest_in_the_scan_then_the_one_observed() -> None:
    scan = [entry(B24, "ZTE", rssi=-70), entry(B5, "ZTE", rssi=-50), entry(OTHER, "X", rssi=-40)]
    assert bssid_order(zte(), scan) == (mac(B5), mac(B24), mac(LAN))
    assert bssid_order(zte(), [], observed=mac(B24)) == (mac(B24), mac(LAN), mac(B5))


def test_without_anything_known_the_listed_order_is_used() -> None:
    assert bssid_order(zte(), None) == (mac(LAN), mac(B24), mac(B5))
    assert bssid_order(Router("x", "X", "#0078D4"), None) == ()


def test_routers_are_found_by_their_address() -> None:
    routers = [zte(address="192.168.1.1"), Router("nb", "Neighbor", "#107C10")]
    assert router_at(routers, "192.168.1.1").id == "zte"
    assert router_at(routers, "10.0.0.1") is None
    assert router_at(routers, None) is None


def test_the_middle_router_setting_and_what_was_learned_are_saved() -> None:
    router = zte(address="192.168.1.1", middle_bssid=mac(B5))
    s = Settings(middle_router="192.168.8.1:8080", routers=(router,))
    again = settings_from_json(settings_to_json(s))
    assert again.middle == Endpoint("192.168.8.1", 8080)
    assert again.routers == (router,)
    assert Settings().middle is None
    with pytest.raises(ValueError, match="middle router"):
        Settings(middle_router="not a router:port")


def test_learned_details_merge_into_the_saved_router() -> None:
    saved = Settings(routers=(zte(),))
    learned = zte(address="192.168.1.1", middle_bssid=mac(B5))
    merged = saved.with_linked_macs([learned]).router("zte")
    assert (merged.address, merged.middle_bssid) == ("192.168.1.1", mac(B5))
    assert saved.with_linked_macs([zte()]).router("zte") == zte()  # nothing learned: kept


# --- spotting a middle router that isn't set up -------------------------------------------


@pytest.mark.parametrize(
    ("address", "private"),
    [
        ("192.168.8.1", True),
        ("10.0.0.1", True),
        ("172.16.0.1", True),
        ("172.31.255.1", True),
        ("172.32.0.1", False),
        ("100.64.0.1", False),  # a provider's shared range: not your router
        ("104.186.104.1", False),
        ("fe80::1", False),
        (None, False),
        ("", False),
    ],
)
def test_is_private_ipv4(address, private) -> None:
    assert is_private_ipv4(address) is private


ZTE = Router("zte", "ZTE", "#0078D4", "ZTE-5G", (mac(LAN), mac(B5)))
MIDDLE_MAC = "88-88-88-88-88-88"


def test_a_cable_into_an_unknown_private_gateway_could_be_a_middle_router() -> None:
    gateway = cable_gateway("192.168.8.1", MIDDLE_MAC)
    assert could_be_middle(gateway, [ZTE], middle_set=False)
    assert not could_be_middle(gateway, [ZTE], middle_set=True)  # already set up
    assert not could_be_middle(gateway, [ZTE], False, dismissed=(MIDDLE_MAC,))
    assert not could_be_middle(cable_gateway("192.168.1.1", LAN), [ZTE], False)  # the ZTE
    assert not could_be_middle(gateway_info("192.168.8.1", MIDDLE_MAC), [ZTE], False)  # Wi-Fi
    assert not could_be_middle(cable_gateway("100.64.0.1", MIDDLE_MAC), [ZTE], False)
    assert not could_be_middle(None, [ZTE], False)
    assert could_be_middle(cable_gateway("192.168.8.1", None), [ZTE], False)  # MAC unknown


def test_gateway_key_is_the_mac_or_else_the_address() -> None:
    assert gateway_key(cable_gateway("192.168.8.1", MIDDLE_MAC)) == MIDDLE_MAC
    assert gateway_key(cable_gateway("192.168.8.1", None)) == "192.168.8.1"


def test_a_private_second_hop_suggests_the_gateway() -> None:
    gateway = cable_gateway("192.168.8.1", MIDDLE_MAC)
    assert middle_suggestion(gateway, "192.168.1.1") == "192.168.8.1"
    assert middle_suggestion(gateway, "104.186.104.1") is None  # the provider: no router between
    assert middle_suggestion(gateway, "100.64.0.1") is None
    assert middle_suggestion(gateway, None) is None
    assert middle_suggestion(gateway, "192.168.8.1") is None


def test_dismissed_gateways_are_saved() -> None:
    s = Settings(middle_dismissed=(MIDDLE_MAC,))
    assert settings_from_json(settings_to_json(s)).middle_dismissed == (MIDDLE_MAC,)
    assert settings_from_json({}).middle_dismissed == ()
