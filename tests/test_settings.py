import json
from dataclasses import replace
from datetime import time

import pytest

from fakes import mac
from router_checker.core.alerts import Thresholds
from router_checker.core.models import Router
from router_checker.core.quiet_hours import QuietHours
from router_checker.core.settings import (
    DEFAULT_TARGETS,
    Settings,
    is_valid_target,
    load_settings,
    save_settings,
    settings_from_json,
    settings_to_json,
)


def test_defaults() -> None:
    s = Settings()
    assert s.interval_min == 5
    assert s.targets == DEFAULT_TARGETS == ("1.1.1.1", "8.8.8.8", "google.com")
    assert s.pings_per_target == 10
    assert s.thresholds == Thresholds(5.0, 100.0, 30.0)
    assert s.retention_days == 30
    assert not s.scheduled_test_all and s.test_all_interval_h == 2


def test_round_trip(tmp_path) -> None:
    router = Router.create("ZTE", ssid="ZTE-Home", macs=[mac("b00ad59a7bb4")])
    s = Settings(
        interval_min=15,
        targets=("9.9.9.9",),
        thresholds=Thresholds(3.0, 80.0, 20.0),
        quiet_hours=QuietHours(True, time(22, 30), time(6, 45)),
        routers=(router,),
    )
    path = tmp_path / "sub" / "settings.json"
    save_settings(path, s)
    assert load_settings(path) == s
    assert json.loads(path.read_text())["routers"][0]["macs"] == ["B0-0A-D5-9A-7B-B4"]


def test_missing_file(tmp_path) -> None:
    assert load_settings(tmp_path / "nope.json") is None


def test_partial_and_unknown_keys() -> None:
    s = settings_from_json({"interval_min": 10, "thresholds": {"jitter_ms": 50}, "future_key": 1})
    assert s.interval_min == 10
    assert s.thresholds == Thresholds(5.0, 100.0, 50.0)
    assert s.targets == DEFAULT_TARGETS


@pytest.mark.parametrize(
    "changes",
    [
        {"interval_min": 7},
        {"targets": ("1.1.1.1", " ")},
        {"pings_per_target": 0},
        {"retention_days": 0},
        {"alert_cooldown_min": 0},
        {"test_all_interval_h": 3},
    ],
)
def test_validation(changes) -> None:
    with pytest.raises(ValueError):
        replace(Settings(), **changes)


def test_router_validation() -> None:
    with pytest.raises(ValueError):
        Router.create(" ")
    with pytest.raises(ValueError):
        Router.create("X", color="blue")


def test_router_add_replace_remove() -> None:
    a = Router.create("A")
    s = Settings().with_router(a)
    a2 = a.with_macs(mac("11-11-11-11-11-11"), mac("11-11-11-11-11-11"))
    s = s.with_router(a2)
    assert s.routers == (a2,)
    assert len(a2.macs) == 1
    assert s.without_router(a.id).routers == ()


def test_new_flags_round_trip(tmp_path) -> None:
    s = Settings(check_on_network_change=False, first_run_done=True)
    path = tmp_path / "s.json"
    save_settings(path, s)
    loaded = load_settings(path)
    assert not loaded.check_on_network_change and loaded.first_run_done
    old_file = settings_from_json({"interval_min": 5})
    assert old_file.check_on_network_change and not old_file.first_run_done


def test_with_linked_macs_keeps_other_changes() -> None:
    a = Router.create("A")
    b = Router.create("B")
    engine_copy = Settings(routers=(a.with_macs(mac("11-11-11-11-11-11")), b))
    user_edited = Settings(interval_min=15, routers=(replace(a, name="Renamed"),))  # B deleted
    merged = user_edited.with_linked_macs([engine_copy.routers[0], engine_copy.routers[1]])
    assert merged.interval_min == 15
    assert merged.routers[0].name == "Renamed"
    assert merged.routers[0].macs == (mac("11-11-11-11-11-11"),)
    assert len(merged.routers) == 1  # a deleted router is not resurrected


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("1.1.1.1", True),
        ("google.com", True),
        ("dns.google", True),
        ("localhost", True),
        (" 8.8.8.8 ", True),
        ("2606:4700::1111", False),  # IPv6 not supported yet
        ("1.2.3", False),
        ("-bad.com", False),
        ("exa mple.com", False),
        ("", False),
    ],
)
def test_is_valid_target(text: str, ok: bool) -> None:
    assert is_valid_target(text) is ok


def test_quiet_hours_json() -> None:
    saved = json.loads(json.dumps(settings_to_json(Settings())))
    assert saved["quiet_hours"] == {"enabled": False, "start": "22:00", "end": "07:00"}
    assert settings_from_json({"quiet_hours": None}).quiet_hours == QuietHours()
    assert settings_from_json({"quiet_hours": {"enabled": True}}).quiet_hours == QuietHours(True)
    # Version 0.2 stored [start hour, end hour] and had no off switch.
    assert settings_from_json({"quiet_hours": [23, 6]}).quiet_hours == QuietHours(
        True, time(23, 0), time(6, 0)
    )
    with pytest.raises(ValueError):
        settings_from_json({"quiet_hours": {"enabled": True, "start": "25:00"}})


def test_test_all_schedule_round_trip(tmp_path) -> None:
    path = tmp_path / "s.json"
    save_settings(path, Settings(scheduled_test_all=True, test_all_interval_h=8))
    loaded = load_settings(path)
    assert loaded.scheduled_test_all and loaded.test_all_interval_h == 8
    assert settings_from_json({}).test_all_interval_h == 2
