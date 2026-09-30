import pytest

from fakes import entry, gateway_info, mac
from router_checker.core.middle import (
    Endpoint,
    bssid_order,
    colon_mac,
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
