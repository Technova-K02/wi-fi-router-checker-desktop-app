import json
from dataclasses import replace

import pytest

from fakes import mac
from router_checker.core.alerts import Thresholds
from router_checker.core.models import Router
from router_checker.core.settings import (
    DEFAULT_TARGETS,
    Settings,
    load_settings,
    save_settings,
    settings_from_json,
)


def test_defaults() -> None:
    s = Settings()
    assert s.interval_min == 5
    assert s.targets == DEFAULT_TARGETS == ("1.1.1.1", "8.8.8.8", "google.com")
    assert s.pings_per_target == 10
    assert s.thresholds == Thresholds(5.0, 100.0, 30.0)
    assert s.retention_days == 30
    assert not s.scheduled_test_all


def test_round_trip(tmp_path) -> None:
    router = Router.create("ZTE", ssid="ZTE-Home", macs=[mac("b00ad59a7bb4")])
    s = Settings(
        interval_min=15,
        targets=("9.9.9.9",),
        thresholds=Thresholds(3.0, 80.0, 20.0),
        quiet_hours=(22, 7),
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
        {"quiet_hours": (22, 24)},
        {"alert_cooldown_min": 0},
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
