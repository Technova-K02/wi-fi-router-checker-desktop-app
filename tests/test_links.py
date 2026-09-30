from router_checker.core.links import Adapter, choose_link
from router_checker.core.models import LinkChoice, LinkKind

AUTO, WIFI_ONLY, ETHERNET_ONLY = LinkChoice.AUTO, LinkChoice.WIFI, LinkChoice.ETHERNET


def adapter(index, kind, *, physical=True, up=True, ip="192.168.1.20", gateway="192.168.1.1"):
    return Adapter(f"{{GUID-{index}}}", f"adapter {index}", index, kind, physical, up, ip, gateway)


WIFI = adapter(9, LinkKind.WIFI)
CABLE = adapter(4, LinkKind.ETHERNET, ip="192.168.0.30", gateway="192.168.0.1")
UNPLUGGED = adapter(5, LinkKind.ETHERNET, up=False, ip=None, gateway=None)
# Virtual adapters Windows lists as Ethernet; some even have an address.
HYPER_V = adapter(22, LinkKind.ETHERNET, physical=False, ip="172.20.0.1", gateway=None)
VMWARE = adapter(20, LinkKind.ETHERNET, physical=False, ip="192.168.80.1", gateway=None)
WIREGUARD = adapter(40, None, physical=False, ip="10.64.0.2", gateway=None)  # a tunnel
OPENVPN_TAP = adapter(41, LinkKind.ETHERNET, physical=False, ip="10.8.0.6", gateway="10.8.0.1")
VIRTUALS = [HYPER_V, VMWARE]


def chosen(adapters, internet, choice=AUTO):
    result = choose_link(adapters, internet.index if internet else None, choice)
    return (result.adapter.index if result.adapter else None), result.vpn


def test_wifi_only_pc() -> None:
    assert chosen([WIFI, UNPLUGGED, *VIRTUALS], WIFI) == (9, False)


def test_ethernet_only_pc_ignores_virtual_adapters() -> None:
    assert chosen([CABLE, *VIRTUALS], CABLE) == (4, False)
    assert chosen([UNPLUGGED, *VIRTUALS], None) == (None, False)


def test_automatic_follows_the_adapter_windows_uses_for_the_internet() -> None:
    both = [WIFI, CABLE, *VIRTUALS]
    assert chosen(both, CABLE) == (4, False)
    assert chosen(both, WIFI) == (9, False)


def test_a_fixed_choice_checks_that_adapter_even_when_the_internet_uses_the_other() -> None:
    both = [WIFI, CABLE]
    assert chosen(both, CABLE, WIFI_ONLY) == (9, False)  # the other one isn't a VPN
    assert chosen(both, WIFI, ETHERNET_ONLY) == (4, False)
    assert chosen([WIFI], WIFI, ETHERNET_ONLY) == (None, False)


def test_with_a_vpn_the_physical_adapter_underneath_is_checked() -> None:
    assert chosen([WIFI, WIREGUARD, *VIRTUALS], WIREGUARD) == (9, True)
    assert chosen([CABLE, OPENVPN_TAP], OPENVPN_TAP) == (4, True)
    # Both physical adapters connected: Ethernet first, as Windows prefers it.
    assert chosen([WIFI, CABLE, WIREGUARD], WIREGUARD) == (4, True)
    assert chosen([WIFI, CABLE, WIREGUARD], WIREGUARD, WIFI_ONLY) == (9, True)


def test_no_route_to_the_internet_still_checks_the_router() -> None:
    assert chosen([CABLE], None) == (4, False)


def test_a_hyper_v_external_switch_carries_the_pcs_address() -> None:
    external = adapter(30, LinkKind.ETHERNET, physical=False)
    card = adapter(3, LinkKind.ETHERNET, ip=None, gateway=None)  # bound to the switch
    assert chosen([card, external, HYPER_V], external) == (30, False)
