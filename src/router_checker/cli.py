"""Console harness: run checks without the UI.

uv run router-checker status
uv run router-checker scan
uv run router-checker check
uv run router-checker watch [--interval 5]
uv run router-checker routers list | add | remove
uv run router-checker profiles
uv run router-checker location
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from router_checker.core.checker import CheckEngine, CycleReport
from router_checker.core.errors import LocationPermissionError, WifiUnavailableError
from router_checker.core.mac import InvalidMacError, MacAddress
from router_checker.core.models import (
    DEFAULT_ROUTER_COLORS,
    Alert,
    GatewayInfo,
    PingStats,
    Router,
)
from router_checker.core.scheduler import INTERVAL_CHOICES_MIN, seconds_until
from router_checker.core.settings import Settings, load_settings, save_settings
from router_checker.core.storage import SqliteHistoryStore

SEED_ROUTER_NAME = "ZTE"
SEED_ROUTER_MAC = "B0-0A-D5-9A-7B-B4"


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} ms"


def _local(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _location_hint() -> str:
    return (
        "Location access is OFF for desktop apps, so Wi-Fi scan and connection details are "
        "unavailable.\n  The current router is still identified by the gateway MAC.\n"
        "  To allow it: run `router-checker location` (opens Settings > Privacy & security > "
        "Location) and turn on 'Let desktop apps access your location'."
    )


# --- settings -------------------------------------------------------------------


class App:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.settings_path = data_dir / "settings.json"
        self.db_path = data_dir / "history.db"

    def load(self) -> Settings:
        settings = load_settings(self.settings_path)
        if settings is None:
            seed = Router.create(SEED_ROUTER_NAME, macs=[MacAddress.parse(SEED_ROUTER_MAC)])
            settings = Settings(routers=(seed,))
            self.save(settings)
            print(f"Created {self.settings_path} with router {seed.name} ({SEED_ROUTER_MAC}).")
        return settings

    def save(self, settings: Settings) -> None:
        save_settings(self.settings_path, settings)

    def engine(self, settings: Settings, store: SqliteHistoryStore) -> CheckEngine:
        from router_checker.platform_windows.icmp import WindowsPingService
        from router_checker.platform_windows.netinfo import WindowsNetworkInfoService
        from router_checker.platform_windows.services import SocketDnsService, SystemClock
        from router_checker.platform_windows.wlan import WindowsWifiService

        return CheckEngine(
            settings,
            wifi=WindowsWifiService(),
            ping=WindowsPingService(),
            dns=SocketDnsService(),
            netinfo=WindowsNetworkInfoService(),
            store=store,
            clock=SystemClock(),
            notifier=ConsoleNotifier(),
        )


class ConsoleNotifier:
    def notify(self, alert: Alert) -> None:
        print(f"\n  *** ALERT: {alert.title} - {alert.text}")


# --- report printing --------------------------------------------------------------


def _router_row(*cols: str) -> str:
    name, state, score, signal, band, crowd, busy = cols
    return f"  {name:<16} {state:<10} {score:<18} {signal:<9} {band:<14} {crowd:>5}  {busy}"


def _ping_row(label: str, stats: PingStats | None, dns_ms: float | None = None) -> str:
    if stats is None:
        return f"  {label:<28} {'DNS failed':>10}"
    dns = "" if dns_ms is None else f"  dns {dns_ms:.1f} ms"
    return (
        f"  {label:<28} {_ms(stats.avg_ms):>10} {_ms(stats.median_ms):>10} "
        f"{_ms(stats.p95_ms):>10} {_ms(stats.jitter_ms):>10} {stats.loss_pct:>5.0f} %{dns}"
    )


def _gateway_line(gw: GatewayInfo) -> str:
    mac = gw.gateway_mac or "?"
    return f"Gateway:  {gw.gateway_ip}  MAC {mac}  ({gw.interface_name}, IP {gw.local_ip})"


def print_report(report: CycleReport) -> None:
    print(f"\n=== Check at {_local(report.timestamp)} ===")
    if not report.location_allowed:
        print("! " + _location_hint())
    if report.wifi_error:
        print(f"! Wi-Fi: {report.wifi_error}")

    conn = report.connection
    if conn:
        bssid = conn.bssid or "?"
        print(f"Wi-Fi:    {conn.ssid!r}  BSSID {bssid}  quality {conn.signal_quality} %")
    gw = report.gateway
    if gw:
        print(_gateway_line(gw))
    else:
        print("Gateway:  none. The Wi-Fi adapter is not connected.")

    match = report.match
    if match.router:
        print(f"Router:   {match.router.name} (matched by {match.method})")
    elif gw:
        print("Router:   not one of your routers. Add it with `router-checker routers add`.")
    if report.linked and match.router:
        new = ", ".join(map(str, match.macs_to_link))
        print(f"          newly linked to {match.router.name}: {new}")

    test = report.test
    if test and test.gateway_ping:
        head = f"  {'target':<28} {'avg':>10} {'median':>10} {'p95':>10} {'jitter':>10}  loss"
        print("\nFull test:")
        print(head)
        print(_ping_row(f"gateway {test.gateway.gateway_ip}", test.gateway_ping))
        for t in test.targets:
            label = t.target if t.address in (None, t.target) else f"{t.target} ({t.address})"
            print(_ping_row(label, t.ping, t.dns.elapsed_ms if t.dns and t.dns.ok else None))

    rec = report.record
    if rec:
        reasons = f" ({', '.join(r.value for r in rec.reasons)})" if rec.reasons else ""
        print(f"\nVerdict:  {rec.verdict.value}{reasons}")
        if rec.gateway_silent:
            print("          (the gateway does not answer ping, so its loss is ignored)")

    if report.statuses:
        print("\nRouters:")
        print(_router_row("name", "state", "score", "signal", "band / ch", "crowd", "busy"))
        for st in report.statuses:
            score = (
                f"{st.score.value} {st.score.label}{' (est.)' if st.score.estimated else ''}"
                if st.score
                else "-"
            )
            obs = st.observation
            signal = f"{obs.entry.rssi} dBm" if obs else "-"
            band = f"{obs.entry.band.value} / {obs.entry.channel}" if obs else "-"
            crowd = str(obs.same_channel_count) if obs else "-"
            busy = "-"
            if obs:
                load = obs.entry.bss_load
                detail = (
                    f"{load.station_count} stations, {load.utilization_pct:.0f} % used"
                    if load
                    else "estimated"
                )
                busy = f"{obs.busyness.level.value} ({detail})"
            flags = (" [current]" if st.is_current else "") + (
                " [Recommended]" if st.recommended else ""
            )
            row = _router_row(st.router.name, st.state.value, score, signal, band, crowd, busy)
            print(row + flags)

    if report.recommendation:
        rec_router = report.settings.router(report.recommendation.router_id)
        margin = (
            f" (+{report.recommendation.margin} points)" if report.recommendation.margin else ""
        )
        print(f"\nRecommended: {rec_router.name if rec_router else '?'}{margin}")
    if report.alert:
        print(f"\nALERT: {report.alert.title} - {report.alert.text}")
    state = "unstable, checking every minute" if report.unstable else "stable"
    print(f"\nNext check: {_local(report.next_check_at)} ({state})")


# --- commands --------------------------------------------------------------------


def cmd_check(app: App, args: argparse.Namespace) -> int:
    settings = app.load()
    if args.count:
        settings = _replace(settings, pings_per_target=args.count)
    store = SqliteHistoryStore(app.db_path)
    try:
        report = app.engine(settings, store).run_cycle()
        if report.linked:
            app.save(app.load().with_linked_macs(report.linked))
        print_report(report)
    finally:
        store.close()
    return 0


def cmd_watch(app: App, args: argparse.Namespace) -> int:
    settings = app.load()
    if args.interval:
        settings = _replace(settings, interval_min=args.interval)
    store = SqliteHistoryStore(app.db_path)
    engine = app.engine(settings, store)
    print(f"Watching every {settings.interval_min} min (1 min while unstable). Ctrl+C to stop.")
    try:
        while True:
            report = engine.run_cycle()
            if report.linked:
                app.save(app.load().with_linked_macs(report.linked))
            print_report(report)
            while (wait := seconds_until(report.next_check_at, engine_now())) > 0:
                time.sleep(min(wait, 1.0))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        store.close()
    return 0


def engine_now() -> datetime:
    from router_checker.platform_windows.services import SystemClock

    return SystemClock().now()


def cmd_status(app: App, _args: argparse.Namespace) -> int:
    from router_checker.core.matching import identify_current
    from router_checker.platform_windows.netinfo import WindowsNetworkInfoService
    from router_checker.platform_windows.wlan import WindowsWifiService

    settings = app.load()
    gw = WindowsNetworkInfoService().wifi_gateway()
    connection = None
    with WindowsWifiService() as wifi:
        try:
            adapters = wifi.interfaces()
            for guid, desc, _state in adapters:
                print(f"Adapter:  {desc}  {guid}")
            connection = wifi.current_connection()
            print("Location: allowed")
        except LocationPermissionError:
            print("! " + _location_hint())
        except WifiUnavailableError as exc:
            print(f"! Wi-Fi: {exc}")
    if connection:
        c = connection
        print(f"Wi-Fi:    {c.ssid!r}  BSSID {c.bssid}  quality {c.signal_quality} %")
        print(f"Profile:  {c.profile_name!r}")
    if gw:
        print(_gateway_line(gw))
    else:
        print("Gateway:  none (Wi-Fi not connected)")
    match = identify_current(settings.routers, connection, gw)
    if match.router:
        print(f"Router:   {match.router.name} (matched by {match.method})")
    else:
        print("Router:   not one of your routers")
    print(f"Data:     {app.data_dir}")
    return 0


def cmd_scan(app: App, _args: argparse.Namespace) -> int:
    from router_checker.core.wifi_info import (
        entry_matches_router,
        entry_same_device,
        same_channel_count,
    )
    from router_checker.platform_windows.wlan import WindowsWifiService

    settings = app.load()
    with WindowsWifiService() as wifi:
        try:
            entries = wifi.scan()
        except LocationPermissionError:
            print("! " + _location_hint())
            return 2
        except WifiUnavailableError as exc:
            print(f"! Wi-Fi: {exc}")
            return 2
    print(_scan_row("SSID", "BSSID", "RSSI", "qual", "band", "ch", "crowd", "BSS load / router"))
    for e in sorted(entries, key=lambda x: x.rssi, reverse=True):
        load = e.bss_load
        load_txt = f"{load.station_count} sta, {load.utilization_pct:.0f} %" if load else "-"
        mine = next((r.name for r in settings.routers if entry_matches_router(e, r)), "")
        if not mine:
            near = next((r.name for r in settings.routers if entry_same_device(e, r)), "")
            mine = f"{near}? (same device as its MAC)" if near else ""
        row = _scan_row(
            (e.ssid or "<hidden>")[:32],
            str(e.bssid),
            f"{e.rssi} dBm",
            f"{e.link_quality}%",
            e.band.value,
            str(e.channel or "-"),
            str(same_channel_count(e, entries)),
            load_txt,
        )
        print(row + (f"  <- {mine}" if mine else ""))
    print(f"\n{len(entries)} BSSs")
    return 0


def _scan_row(*cols: str) -> str:
    ssid, bssid, rssi, qual, band, ch, crowd, load = cols
    return f"{ssid:<32} {bssid:<18} {rssi:>8} {qual:>5} {band:>8} {ch:>4} {crowd:>5}  {load}"


def cmd_profiles(_app: App, _args: argparse.Namespace) -> int:
    from router_checker.platform_windows.wlan import WindowsWifiService

    with WindowsWifiService() as wifi:
        for name in wifi.saved_profiles():
            print(name)
    return 0


def cmd_location(_app: App, _args: argparse.Namespace) -> int:
    from router_checker.platform_windows.services import open_location_settings

    open_location_settings()
    print("Opened Settings > Privacy & security > Location.")
    return 0


def cmd_routers(app: App, args: argparse.Namespace) -> int:
    settings = app.load()
    if args.action == "list":
        for r in settings.routers:
            macs = ", ".join(map(str, r.macs)) or "-"
            print(f"{r.id[:8]}  {r.name:<16} {r.color}  ssid={r.ssid or '-'}  macs={macs}")
        return 0
    if args.action == "add":
        try:
            macs = [MacAddress.parse(m) for m in args.mac or []]
        except InvalidMacError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not macs and not args.ssid:
            print("error: give at least --mac or --ssid", file=sys.stderr)
            return 2
        color = (
            args.color or DEFAULT_ROUTER_COLORS[len(settings.routers) % len(DEFAULT_ROUTER_COLORS)]
        )
        try:
            router = Router.create(args.name, color=color, ssid=args.ssid, macs=macs)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        app.save(settings.with_router(router))
        print(f"Added {router.name} ({router.id[:8]}).")
        return 0
    # remove
    matches = [r for r in settings.routers if r.id.startswith(args.router) or r.name == args.router]
    if len(matches) != 1:
        print(
            f"error: {'no' if not matches else 'more than one'} router matches {args.router!r}",
            file=sys.stderr,
        )
        return 2
    app.save(settings.without_router(matches[0].id))
    print(f"Removed {matches[0].name}.")
    return 0


def _replace(settings: Settings, **changes: object) -> Settings:
    from dataclasses import replace

    return replace(settings, **changes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="router-checker", description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, help="default: %%LOCALAPPDATA%%\\RouterChecker")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="adapter, connection, gateway and current router")
    sub.add_parser("scan", help="nearby Wi-Fi networks")
    check = sub.add_parser("check", help="run one full check cycle and store it")
    check.add_argument("--count", type=int, help="pings per target (default from settings)")
    watch = sub.add_parser("watch", help="run checks on the timer")
    watch.add_argument("--interval", type=int, choices=INTERVAL_CHOICES_MIN)
    sub.add_parser("profiles", help="Wi-Fi profiles saved in Windows")
    sub.add_parser("location", help="open the Windows location privacy settings")

    routers = sub.add_parser("routers", help="manage your routers")
    rsub = routers.add_subparsers(dest="action", required=True)
    rsub.add_parser("list")
    add = rsub.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--mac", action="append", help="repeat for several MACs")
    add.add_argument("--ssid")
    add.add_argument("--color", help="#RRGGBB")
    remove = rsub.add_parser("remove")
    remove.add_argument("router", help="name or id prefix")
    return parser


COMMANDS = {
    "status": cmd_status,
    "scan": cmd_scan,
    "check": cmd_check,
    "watch": cmd_watch,
    "profiles": cmd_profiles,
    "location": cmd_location,
    "routers": cmd_routers,
}


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if sys.platform != "win32" and args.command not in ("routers",):
        print("This command needs Windows.", file=sys.stderr)
        return 1
    from router_checker.platform_windows.services import data_dir

    app = App(args.data_dir or data_dir())
    return COMMANDS[args.command](app, args)


if __name__ == "__main__":
    raise SystemExit(main())
