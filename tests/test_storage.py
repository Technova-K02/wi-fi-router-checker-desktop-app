from datetime import timedelta

import pytest

from fakes import T0, entry, record
from router_checker.core.models import (
    BssLoad,
    BusyLevel,
    Busyness,
    Event,
    InstabilityReason,
    ScanObservation,
    Verdict,
)
from router_checker.core.storage import SqliteHistoryStore


@pytest.fixture
def store():
    s = SqliteHistoryStore(":memory:")
    yield s
    s.close()


def test_checks_round_trip(store) -> None:
    r = record(
        verdict=Verdict.UNSTABLE,
        reasons=(InstabilityReason.HIGH_JITTER, InstabilityReason.HIGH_LOSS),
        score=72.5,
        gateway_silent=True,
    )
    store.add_check(r)
    store.add_check(record(router_id="other"))
    assert store.checks("r1", T0 - timedelta(minutes=1)) == [r]
    assert store.checks("r1", T0 + timedelta(seconds=1)) == []


def test_observations(store) -> None:
    load = BssLoad(5, 100)
    obs = ScanObservation(
        T0, "r1", entry("AA-BB-CC-DD-EE-01", load=load), 3, Busyness(0.4, BusyLevel.MEDIUM, False)
    )
    newer = ScanObservation(
        T0 + timedelta(minutes=5),
        "r1",
        entry("AA-BB-CC-DD-EE-02"),
        1,
        Busyness(0.1, BusyLevel.LOW, True),
    )
    store.add_observations([obs, newer])
    assert store.latest_observation("r1") == newer
    assert store.latest_observation("nope") is None
    store.purge(T0 + timedelta(minutes=1))
    assert store.latest_observation("r1") == newer


def test_bss_load_round_trip(store) -> None:
    obs = ScanObservation(
        T0,
        "r1",
        entry("AA-BB-CC-DD-EE-01", load=BssLoad(5, 100)),
        3,
        Busyness(0.4, BusyLevel.MEDIUM, False),
    )
    store.add_observations([obs])
    assert store.latest_observation("r1").entry.bss_load == BssLoad(5, 100)


def test_hourly_aggregates(store) -> None:
    store.add_hourly("r1", T0 + timedelta(minutes=5), 0.2, 80.0)
    store.add_hourly("r1", T0 + timedelta(minutes=50), 0.4, None)
    store.add_hourly("r1", T0 + timedelta(minutes=65), None, None)
    rows = store.hourly("r1", T0)
    assert [r.hour_start for r in rows] == [T0, T0 + timedelta(hours=1)]
    assert rows[0].busyness_avg == pytest.approx(0.3)
    assert rows[0].score_avg == 80.0
    assert rows[1].busyness_avg is None and rows[1].score_avg is None


def test_events_newest_first_and_filter(store) -> None:
    store.add_event(Event(T0, "r1", "unstable", "a"))
    store.add_event(Event(T0 + timedelta(minutes=1), "r2", "linked", "b"))
    store.add_event(Event(T0 + timedelta(minutes=2), "r1", "recovered", "c"))
    assert [e.message for e in store.events(None, 10)] == ["c", "b", "a"]
    assert [e.message for e in store.events("r1", 1)] == ["c"]


def test_purge(store) -> None:
    store.add_check(record())
    store.add_hourly("r1", T0, 0.5, 50.0)
    store.add_event(Event(T0, "r1", "x", "y"))
    store.purge(T0 + timedelta(days=1))
    assert store.checks("r1", T0 - timedelta(days=1)) == []
    assert store.hourly("r1", T0 - timedelta(days=1)) == []
    assert store.events(None, 10) == []


def test_file_store_persists(tmp_path) -> None:
    path = tmp_path / "data" / "history.db"
    s = SqliteHistoryStore(path)
    s.add_check(record())
    s.close()
    s = SqliteHistoryStore(path)
    assert len(s.checks("r1", T0)) == 1
    s.close()
