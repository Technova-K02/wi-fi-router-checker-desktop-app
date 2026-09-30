"""Test all and switching behind the middle router: planning, switching, going back."""

import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from fakes import (
    CAFE_BSSID,
    MIDDLE,
    NB_ADDRESS,
    NB_BSSID,
    T0,
    ZTE_ADDRESS,
    ZTE_BSSID,
    ZTE_LAN,
    FakeWifi,
    entry,
    mac,
    middle_parts,
    middle_routers,
)
from router_checker.core.checker import CheckEngine
from router_checker.core.middle_switching import (
    NOT_BEHIND,
    MiddleMarker,
    MiddleMarkerFile,
    MiddleRunner,
    gather_middle_plan,
    no_way_back,
    plan_middle,
    unknown_origin,
)
from router_checker.core.models import Router
from router_checker.core.settings import Settings
from router_checker.core.switching import (
    TEST_ALL_EVENT,
    MiddleOrigin,
    SkipReason,
    Stage,
    SwitchTiming,
    run_event_text,
)

FAST = SwitchTiming(connect_timeout_s=0.3, settle_s=0.0, poll_s=0.01)
CAFE_ADDRESS = "10.1.1.1"
CAFE = Router("cafe", "Cafe", "#8764B8", macs=(mac(CAFE_BSSID),))


@pytest.fixture
def parts():
    parts = middle_parts()
    parts["middle"].joins[CAFE_BSSID] = CAFE_ADDRESS
    parts["ping"].replies[CAFE_ADDRESS] = [8.0]
    return parts


def routers():
    return (*middle_routers(), CAFE)


def engine_for(parts, routers_=None):
    return CheckEngine(
        Settings(pings_per_target=4, routers=routers_ or routers(), middle_router=f"{MIDDLE}:8080"),
        wifi=parts["wifi"],
        ping=parts["ping"],
        dns=parts["dns"],
        netinfo=parts["netinfo"],
        store=parts["store"],
        clock=parts["clock"],
    )


def runner_for(parts, tmp_path, routers_=None):
    engine = engine_for(parts, routers_)
    marker = MiddleMarkerFile(tmp_path / "middle-restore.json")
    runner = MiddleRunner(
        engine, parts["middle"], parts["netinfo"], parts["store"], parts["clock"], marker, FAST
    )
    return runner, engine, marker


def plan_now(parts, engine):
    return gather_middle_plan(engine, parts["netinfo"], parts["wifi"], parts["store"])


# --- planning ----------------------------------------------------------------------


def test_the_plan_starts_from_the_router_the_middle_router_is_on() -> None:
    scan = [entry(ZTE_BSSID, "ZTE-Home"), entry(NB_BSSID, "Neighbor"), entry(CAFE_BSSID, "Cafe")]
    plan = plan_middle(routers(), ZTE_ADDRESS, scan)
    assert plan.via_middle and plan.blocker is None
    assert plan.origin == MiddleOrigin(plan.current, ZTE_ADDRESS, (mac(ZTE_BSSID), mac(ZTE_LAN)))
    assert plan.current.id == "zte"
    assert [(c.router.id, c.bssids) for c in plan.to_test] == [
        ("nb", (mac(NB_BSSID),)),
        ("cafe", (mac(CAFE_BSSID),)),
    ]
    assert [(s.router.id, s.reason) for s in plan.skipped] == [("gone", SkipReason.NO_WIFI_MAC)]


def test_routers_out_of_range_are_skipped_when_this_pc_can_scan() -> None:
    plan = plan_middle(routers(), ZTE_ADDRESS, [entry(NB_BSSID, "Neighbor")])
    assert [c.router.id for c in plan.to_test] == ["nb"]
    assert (plan.skipped[-1].router.id, plan.skipped[-1].reason) == (
        "cafe", SkipReason.NOT_IN_RANGE
    )  # fmt: skip
    # Without a scan (no Wi-Fi on this PC) every router with a Wi-Fi MAC is tried.
    assert [c.router.id for c in plan_middle(routers(), ZTE_ADDRESS, None).to_test] == [
        "nb", "cafe"
    ]  # fmt: skip


def test_without_knowing_where_it_is_nothing_can_switch() -> None:
    assert plan_middle(routers(), None, None).blocker == unknown_origin(None)
    assert plan_middle(routers(), "172.16.0.1", None).blocker == unknown_origin("172.16.0.1")
    bare = replace(routers()[0], macs=())
    assert plan_middle((bare, *routers()[1:]), ZTE_ADDRESS, None).blocker == no_way_back(bare)


def test_gathering_the_plan(parts) -> None:
    engine = engine_for(parts)
    plan = plan_now(parts, engine)
    assert plan.current.id == "zte" and [c.router.id for c in plan.to_test] == ["nb", "cafe"]
    parts["netinfo"].cable = None  # on the ZTE's Wi-Fi: not behind the middle router
    assert plan_now(parts, engine).blocker == NOT_BEHIND


def test_without_wifi_every_router_with_a_wifi_mac_is_planned(parts) -> None:
    parts["wifi"] = FakeWifi(missing=True)
    plan = plan_now(parts, engine_for(parts))
    assert [c.router.id for c in plan.to_test] == ["nb", "cafe"]


# --- Test all ----------------------------------------------------------------------


def run_all(runner, plan, *, cancel=None, exiting=None, progress=None):
    return runner.test_all(
        plan,
        cancel=cancel or threading.Event(),
        exiting=exiting or threading.Event(),
        progress=progress or (lambda _p: None),
    )


def test_test_all_switches_the_middle_router_and_always_goes_back(parts, tmp_path) -> None:
    fresh_nb = replace(middle_routers()[1], address=None)  # never switched to before
    rs = (routers()[0], fresh_nb, routers()[2], CAFE)
    runner, engine, marker = runner_for(parts, tmp_path, rs)
    stages = []
    result = run_all(runner, plan_now(parts, engine), progress=stages.append)
    assert [o.router.id for o in result.tested] == ["nb", "cafe"]
    assert result.restored and not result.cancelled
    assert parts["middle"].calls == [NB_BSSID, CAFE_BSSID, ZTE_BSSID]
    assert parts["ping"].upstream == ZTE_ADDRESS
    assert [(p.stage, p.name) for p in stages][-1] == (Stage.RESTORING, "ZTE")
    nb_record = result.tested[0].record
    assert nb_record.gateway_avg_ms == 4.0 and nb_record.router_id == "nb"  # the Neighbor
    learned = {r.id: r for r in result.linked}
    assert (learned["nb"].address, learned["nb"].middle_bssid) == (NB_ADDRESS, mac(NB_BSSID))
    assert learned["cafe"].address == CAFE_ADDRESS
    assert not marker.path.exists()
    text = parts["store"].events(None, 5)[0]
    assert text.kind == TEST_ALL_EVENT
    assert text.message.endswith("; the middle router is back on ZTE")


def test_the_next_wifi_mac_is_tried_when_the_first_doesnt_work(parts, tmp_path) -> None:
    other = "22-22-22-22-22-2A"  # a MAC the middle router can't find
    nb = replace(middle_routers()[1], macs=(mac(other), mac(NB_BSSID)), address=None)
    rs = (routers()[0], nb, routers()[2])
    parts["wifi"] = FakeWifi(missing=True)  # no scan to say which MAC is on the air
    runner, engine, _ = runner_for(parts, tmp_path, rs)
    result = run_all(runner, plan_now(parts, engine))
    assert [o.router.id for o in result.tested] == ["nb"]
    assert parts["middle"].calls == [other, NB_BSSID, ZTE_BSSID]
    assert result.linked[0].middle_bssid == mac(NB_BSSID)  # remembered for next time


def test_a_router_it_cant_reach_is_reported_and_the_rest_go_on(parts, tmp_path) -> None:
    parts["middle"].joins.pop(NB_BSSID)
    runner, engine, _ = runner_for(parts, tmp_path)
    result = run_all(runner, plan_now(parts, engine))
    assert [o.router.id for o in result.failed] == ["nb"]
    assert [o.router.id for o in result.tested] == ["cafe"]
    assert result.restored


def test_cancel_goes_back_right_after_the_current_router(parts, tmp_path) -> None:
    runner, engine, marker = runner_for(parts, tmp_path)
    cancel = threading.Event()
    parts["middle"].on_change = lambda m: cancel.set() if m == NB_BSSID else None
    result = run_all(runner, plan_now(parts, engine), cancel=cancel)
    assert result.cancelled and result.restored
    assert parts["middle"].calls == [NB_BSSID, ZTE_BSSID]
    assert not marker.path.exists()


def test_on_exit_it_only_asks_and_keeps_the_marker(parts, tmp_path) -> None:
    runner, engine, marker = runner_for(parts, tmp_path)
    exiting, cancel = threading.Event(), threading.Event()

    def exit_on_nb(m):
        if m == NB_BSSID:
            cancel.set()
            exiting.set()

    parts["middle"].on_change = exit_on_nb
    result = run_all(runner, plan_now(parts, engine), cancel=cancel, exiting=exiting)
    assert result.exited and not result.restored
    assert parts["middle"].calls == [NB_BSSID, ZTE_BSSID]  # asked to go back, didn't wait
    assert marker.load().origin_id == "zte"
    assert "only asked the middle router for ZTE" in run_event_text(result, False)


# --- one switch --------------------------------------------------------------------


def test_switching_learns_what_worked(parts, tmp_path) -> None:
    fresh_nb = replace(middle_routers()[1], address=None)
    rs = (routers()[0], fresh_nb, routers()[2], CAFE)
    runner, engine, _ = runner_for(parts, tmp_path, rs)
    result = runner.switch(plan_now(parts, engine), fresh_nb, stop=threading.Event())
    assert result.ok and parts["ping"].upstream == NB_ADDRESS
    assert (result.linked.address, result.linked.middle_bssid) == (NB_ADDRESS, mac(NB_BSSID))
    assert parts["store"].events("nb", 1)[0].message == "Switched to Neighbor"


def test_a_failed_switch_goes_back(parts, tmp_path) -> None:
    parts["middle"].rejects.add(NB_BSSID)
    runner, engine, _ = runner_for(parts, tmp_path)
    result = runner.switch(plan_now(parts, engine), routers()[1], stop=threading.Event())
    assert not result.ok and result.restored
    assert result.problem == "the middle router didn't accept the switch (HTTP 400)"
    assert parts["ping"].upstream == ZTE_ADDRESS


def test_a_middle_router_that_is_down(parts, tmp_path) -> None:
    parts["middle"].down = True
    runner, engine, _ = runner_for(parts, tmp_path)
    result = runner.switch(plan_now(parts, engine), routers()[1], stop=threading.Event())
    assert not result.ok and result.restored  # still on the ZTE, so it's "back"


# --- after an interrupted run -------------------------------------------------------


def marker_for(started=T0):
    return MiddleMarker(started, "zte", ZTE_ADDRESS, (mac(ZTE_BSSID),), ("nb", "cafe"))


def test_the_next_start_switches_back_from_a_tested_router(parts, tmp_path) -> None:
    runner, _, marker = runner_for(parts, tmp_path)
    marker.save(marker_for())
    parts["ping"].upstream = NB_ADDRESS  # the app died while on the Neighbor
    event = runner.recover(stop=threading.Event())
    assert event.message == "Switched the middle router back to ZTE after an interrupted Test all"
    assert parts["ping"].upstream == ZTE_ADDRESS
    assert not marker.path.exists()


@pytest.mark.parametrize(
    ("upstream", "started"),
    [
        (ZTE_ADDRESS, T0),  # already back
        ("172.16.0.1", T0),  # somewhere else: you moved on
        (NB_ADDRESS, T0 - timedelta(hours=3)),  # too long ago
    ],
)
def test_the_next_start_leaves_other_cases_alone(parts, tmp_path, upstream, started) -> None:
    runner, _, marker = runner_for(parts, tmp_path)
    marker.save(marker_for(started))
    parts["ping"].upstream = upstream
    assert runner.recover(stop=threading.Event()) is None
    assert parts["middle"].calls == []
    assert not marker.path.exists()


def test_the_marker_file(tmp_path) -> None:
    marker = MiddleMarkerFile(tmp_path / "m.json")
    assert marker.load() is None
    marker.save(marker_for())
    assert marker.load() == marker_for()
    marker.path.write_text("{not json", encoding="utf-8")
    assert marker.load() is None
