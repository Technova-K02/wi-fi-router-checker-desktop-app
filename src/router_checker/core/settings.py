"""User settings, stored as JSON (routers included).

Unknown keys are ignored and missing keys fall back to defaults, so older
settings files keep loading after upgrades.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from router_checker.core.alerts import (
    DEFAULT_COOLDOWN_MIN,
    DEFAULT_RECOVERY_CHECKS,
    DEFAULT_UNSTABLE_CHECKS,
    Thresholds,
)
from router_checker.core.mac import MacAddress
from router_checker.core.models import Router
from router_checker.core.scheduler import DEFAULT_INTERVAL_MIN, INTERVAL_CHOICES_MIN

DEFAULT_TARGETS = ("1.1.1.1", "8.8.8.8", "google.com")
DEFAULT_PINGS = 10
DEFAULT_PING_TIMEOUT_MS = 1000
DEFAULT_PING_SPACING_MS = 200
DEFAULT_RETENTION_DAYS = 30

_HOST_LABEL = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}$){_HOST_LABEL}(\.{_HOST_LABEL})*\.?$")


def is_valid_target(text: str) -> bool:
    """An IPv4 address or a host name such as ``google.com`` (no IPv6 yet)."""
    text = text.strip()
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return bool(_HOSTNAME.match(text)) and not text.replace(".", "").isdigit()
    return True


@dataclass(frozen=True, slots=True)
class Settings:
    interval_min: int = DEFAULT_INTERVAL_MIN
    targets: tuple[str, ...] = DEFAULT_TARGETS
    pings_per_target: int = DEFAULT_PINGS
    ping_timeout_ms: int = DEFAULT_PING_TIMEOUT_MS
    ping_spacing_ms: int = DEFAULT_PING_SPACING_MS
    thresholds: Thresholds = field(default_factory=Thresholds)
    unstable_checks: int = DEFAULT_UNSTABLE_CHECKS
    recovery_checks: int = DEFAULT_RECOVERY_CHECKS
    alert_cooldown_min: int = DEFAULT_COOLDOWN_MIN
    notifications_enabled: bool = True
    quiet_hours: tuple[int, int] | None = None  # (start hour, end hour), local time
    start_with_windows: bool = False
    scheduled_test_all: bool = False
    retention_days: int = DEFAULT_RETENTION_DAYS
    check_on_network_change: bool = True
    first_run_done: bool = False
    routers: tuple[Router, ...] = ()

    def __post_init__(self) -> None:
        if self.interval_min not in INTERVAL_CHOICES_MIN:
            raise ValueError(f"interval must be one of {INTERVAL_CHOICES_MIN} minutes")
        if any(not t.strip() for t in self.targets):
            raise ValueError("targets must not be empty strings")
        if not 1 <= self.pings_per_target <= 100:
            raise ValueError("pings per target must be between 1 and 100")
        if self.ping_timeout_ms < 100 or self.ping_spacing_ms < 0:
            raise ValueError("ping timeout must be >= 100 ms and spacing >= 0")
        if min(self.unstable_checks, self.recovery_checks, self.alert_cooldown_min) < 1:
            raise ValueError("alert counts and cooldown must be at least 1")
        if self.retention_days < 1:
            raise ValueError("history retention must be at least 1 day")
        if self.quiet_hours is not None and not all(0 <= h <= 23 for h in self.quiet_hours):
            raise ValueError("quiet hours must be between 0 and 23")

    def router(self, router_id: str) -> Router | None:
        return next((r for r in self.routers if r.id == router_id), None)

    def with_router(self, router: Router) -> Settings:
        """Add the router, or replace the one with the same id."""
        others = tuple(r for r in self.routers if r.id != router.id)
        if len(others) == len(self.routers):
            return replace(self, routers=(*self.routers, router))
        return replace(
            self, routers=tuple(router if r.id == router.id else r for r in self.routers)
        )

    def without_router(self, router_id: str) -> Settings:
        return replace(self, routers=tuple(r for r in self.routers if r.id != router_id))

    def with_linked_macs(self, routers: Iterable[Router]) -> Settings:
        """Add MACs the engine linked, keeping every other change made meanwhile."""
        result = self
        for router in routers:
            existing = result.router(router.id)
            if existing is not None:
                result = result.with_router(existing.with_macs(*router.macs))
        return result


def _router_to_json(r: Router) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "color": r.color,
        "ssid": r.ssid,
        "macs": [str(m) for m in r.macs],
    }


def _router_from_json(d: dict[str, Any]) -> Router:
    return Router(
        id=str(d["id"]),
        name=str(d["name"]),
        color=str(d["color"]),
        ssid=d.get("ssid") or None,
        macs=tuple(MacAddress.parse(m) for m in d.get("macs", [])),
    )


def settings_to_json(s: Settings) -> dict[str, Any]:
    return {
        "interval_min": s.interval_min,
        "targets": list(s.targets),
        "pings_per_target": s.pings_per_target,
        "ping_timeout_ms": s.ping_timeout_ms,
        "ping_spacing_ms": s.ping_spacing_ms,
        "thresholds": {
            "loss_pct": s.thresholds.loss_pct,
            "gateway_p95_ms": s.thresholds.gateway_p95_ms,
            "jitter_ms": s.thresholds.jitter_ms,
        },
        "unstable_checks": s.unstable_checks,
        "recovery_checks": s.recovery_checks,
        "alert_cooldown_min": s.alert_cooldown_min,
        "notifications_enabled": s.notifications_enabled,
        "quiet_hours": list(s.quiet_hours) if s.quiet_hours else None,
        "start_with_windows": s.start_with_windows,
        "scheduled_test_all": s.scheduled_test_all,
        "retention_days": s.retention_days,
        "check_on_network_change": s.check_on_network_change,
        "first_run_done": s.first_run_done,
        "routers": [_router_to_json(r) for r in s.routers],
    }


def settings_from_json(data: dict[str, Any]) -> Settings:
    defaults = Settings()
    kwargs: dict[str, Any] = {}
    for key in (
        "interval_min",
        "pings_per_target",
        "ping_timeout_ms",
        "ping_spacing_ms",
        "unstable_checks",
        "recovery_checks",
        "alert_cooldown_min",
        "retention_days",
    ):
        if key in data:
            kwargs[key] = int(data[key])
    for key in (
        "notifications_enabled",
        "start_with_windows",
        "scheduled_test_all",
        "check_on_network_change",
        "first_run_done",
    ):
        if key in data:
            kwargs[key] = bool(data[key])
    if "targets" in data:
        kwargs["targets"] = tuple(str(t).strip() for t in data["targets"])
    if "thresholds" in data:
        t = data["thresholds"]
        d = defaults.thresholds
        kwargs["thresholds"] = Thresholds(
            loss_pct=float(t.get("loss_pct", d.loss_pct)),
            gateway_p95_ms=float(t.get("gateway_p95_ms", d.gateway_p95_ms)),
            jitter_ms=float(t.get("jitter_ms", d.jitter_ms)),
        )
    if data.get("quiet_hours"):
        start, end = data["quiet_hours"]
        kwargs["quiet_hours"] = (int(start), int(end))
    if "routers" in data:
        kwargs["routers"] = tuple(_router_from_json(r) for r in data["routers"])
    return Settings(**kwargs)


def load_settings(path: Path) -> Settings | None:
    """Settings from ``path``, or None if the file does not exist yet."""
    if not path.exists():
        return None
    return settings_from_json(json.loads(path.read_text(encoding="utf-8")))


def save_settings(path: Path, settings: Settings) -> None:
    """Write atomically, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings_to_json(settings), indent=2), encoding="utf-8")
    os.replace(tmp, path)
