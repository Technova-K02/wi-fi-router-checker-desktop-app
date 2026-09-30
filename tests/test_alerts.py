from dataclasses import replace
from datetime import timedelta

import pytest

from fakes import T0
from router_checker.core.alerts import AlertEngine, AlertSnapshot, Thresholds, diagnose
from router_checker.core.models import (
    Alert,
    AlertKind,
    InstabilityReason,
    Score,
    TargetResult,
    Verdict,
)
from router_checker.core.stats import summarize

GOOD = [10.0] * 10
R = InstabilityReason


def target(samples: list[float | None] | None) -> TargetResult:
    return TargetResult("t", "1.1.1.1", None if samples is None else summarize(samples))


DEFAULT = Thresholds()


def run(gw, targets, loss=0.0, jitter=0.0, th=DEFAULT):
    return diagnose(None if gw is None else summarize(gw), tuple(targets), loss, jitter, th)


def test_ok() -> None:
    d = run(GOOD, [target(GOOD)])
    assert d.verdict is Verdict.OK
    assert d.reasons == ()


def test_not_connected() -> None:
    assert run(None, []).verdict is Verdict.NOT_CONNECTED


def test_gateway_answers_all_targets_fail_is_internet_problem() -> None:
    d = run(GOOD, [target([None] * 10), target(None)], loss=100.0, jitter=None)
    assert d.verdict is Verdict.INTERNET_DOWN
    assert d.reasons == (R.ALL_TARGETS_FAILED,)  # not HIGH_LOSS as well


def test_gateway_and_targets_fail_is_router_problem() -> None:
    d = run([None] * 10, [target([None] * 10)], loss=100.0, jitter=None)
    assert d.verdict is Verdict.ROUTER_UNREACHABLE
    assert d.reasons == (R.GATEWAY_UNREACHABLE,)


def test_silent_gateway_is_ignored_when_internet_works() -> None:
    d = run([None] * 10, [target(GOOD)])
    assert d.verdict is Verdict.OK
    assert d.gateway_silent


@pytest.mark.parametrize(
    ("gw", "loss", "jitter", "reason"),
    [
        ([10.0] * 9 + [None], 0.0, 0.0, R.HIGH_LOSS),  # 10 % gateway loss
        (GOOD, 5.0, 0.0, R.HIGH_LOSS),  # exactly 5 % internet loss
        ([10.0] * 8 + [150.0, 150.0], 0.0, 0.0, R.HIGH_GATEWAY_LATENCY),
        (GOOD, 0.0, 30.5, R.HIGH_JITTER),
    ],
)
def test_unstable_rules(gw, loss, jitter, reason) -> None:
    d = run(gw, [target(GOOD)], loss=loss, jitter=jitter)
    assert d.verdict is Verdict.UNSTABLE
    assert reason in d.reasons


def test_boundaries_are_not_unstable() -> None:
    assert run(GOOD, [target(GOOD)], loss=4.9, jitter=30.0).verdict is Verdict.OK


def test_custom_thresholds() -> None:
    th = Thresholds(loss_pct=50, gateway_p95_ms=500, jitter_ms=100)
    assert run([10.0] * 9 + [None], [target(GOOD)], loss=20, jitter=50, th=th).verdict is Verdict.OK
    with pytest.raises(ValueError):
        Thresholds(loss_pct=0)


# --- AlertEngine ---------------------------------------------------------------

BAD = (Verdict.UNSTABLE, (R.HIGH_LOSS,))
OK = (Verdict.OK, ())


def feed(engine: AlertEngine, seq, start=T0, step_min=1):
    out = []
    for i, (verdict, reasons) in enumerate(seq):
        out.append(
            engine.process("r1", "Home", start + timedelta(minutes=i * step_min), verdict, reasons)
        )
    return out


def kinds(alerts):
    return [a.kind if a else None for a in alerts]


def test_alert_after_two_unstable_in_a_row() -> None:
    engine = AlertEngine()
    assert kinds(feed(engine, [BAD, OK, BAD, BAD])) == [None, None, None, AlertKind.UNSTABLE]


def test_cooldown_then_reminder() -> None:
    engine = AlertEngine()
    alerts = feed(engine, [BAD] * 17)  # one per minute
    fired = [i for i, a in enumerate(alerts) if a]
    assert fired == [1, 16]  # 15-minute cooldown


def test_back_to_normal_after_two_good_checks() -> None:
    engine = AlertEngine()
    alerts = feed(engine, [BAD, BAD, OK, OK, OK])
    assert kinds(alerts) == [None, AlertKind.UNSTABLE, None, AlertKind.RECOVERED, None]
    assert alerts[3].title == "Home is back to normal"


def test_no_recovery_without_alert() -> None:
    assert kinds(feed(AlertEngine(), [BAD, OK, OK])) == [None, None, None]


def test_flapping_within_cooldown_does_not_realert() -> None:
    engine = AlertEngine()
    alerts = feed(engine, [BAD, BAD, OK, OK, BAD, BAD])
    assert kinds(alerts) == [None, AlertKind.UNSTABLE, None, AlertKind.RECOVERED, None, None]


def test_cooldown_is_per_router() -> None:
    engine = AlertEngine()
    for rid in ("a", "b"):
        engine.process(rid, rid, T0, *BAD)
    assert engine.process("a", "a", T0, *BAD) is not None
    assert engine.process("b", "b", T0, *BAD) is not None


def test_is_unstable_until_recovery_confirmed() -> None:
    engine = AlertEngine()
    feed(engine, [BAD, BAD, OK])
    assert engine.is_unstable("r1")
    feed(engine, [OK], start=T0 + timedelta(minutes=10))
    assert not engine.is_unstable("r1")


def test_internet_down_alert_title() -> None:
    engine = AlertEngine(unstable_checks=1)
    alert = engine.process("r1", "Home", T0, Verdict.INTERNET_DOWN, (R.ALL_TARGETS_FAILED,))
    assert alert.title == "Internet provider problem on Home"
    assert alert.message == "The router answers, but no internet target does."


def test_alert_texts() -> None:
    unstable = Alert(
        AlertKind.UNSTABLE, "r1", "Home", T0, Verdict.UNSTABLE, (R.HIGH_LOSS, R.HIGH_JITTER)
    )
    assert unstable.title == "Home is unstable"
    assert unstable.message == "High packet loss, high jitter."
    assert unstable.suggestion == "" and unstable.text == unstable.message

    better = replace(unstable, recommended_name="Cafe", recommended_score=Score(85, True))
    assert better.suggestion == "Try Cafe instead (score ~85)."
    assert better.text == "High packet loss, high jitter. Try Cafe instead (score ~85)."

    down = Alert(
        AlertKind.UNSTABLE, "r1", "Home", T0, Verdict.ROUTER_UNREACHABLE, (R.GATEWAY_UNREACHABLE,)
    )
    assert down.title == "Home is not responding"
    assert down.message == "Neither the router nor the internet answered."

    recovered = replace(better, kind=AlertKind.RECOVERED)
    assert recovered.message == "The last checks were stable." and recovered.suggestion == ""


def test_snapshot_reports_streaks() -> None:
    engine = AlertEngine()
    assert engine.snapshot("r1") == AlertSnapshot(0, 0, False)
    feed(engine, [BAD, BAD, OK])
    assert engine.snapshot("r1") == AlertSnapshot(0, 1, True)
