"""Automatic switching rules."""

from datetime import timedelta

from fakes import T0
from router_checker.core.auto_switch import (
    Action,
    AutoSwitchPolicy,
    CheckView,
    Decision,
    Reason,
)
from router_checker.core.models import Recommendation, Score, Verdict
from router_checker.core.presentation import (
    StatusLevel,
    auto_switch_message,
    auto_switch_progress,
    auto_switch_reason,
)

NAMES = {"zte": "ZTE", "cafe": "Cafe", "nb": "Neighbor"}
CAFE = Recommendation("cafe", Score(88, False), Score(61, False), 27)
CANDIDATES = {"cafe": Score(88, False), "nb": Score(70, True)}


def check(minute, rec=CAFE, verdict=Verdict.OK, current="zte", candidates=CANDIDATES):
    return CheckView(T0 + timedelta(minutes=minute), current, verdict, rec, candidates)


def test_switches_after_three_better_checks_in_a_row() -> None:
    policy = AutoSwitchPolicy()
    assert policy.observe(check(0)).action is Action.STAY
    assert policy.observe(check(5)).action is Action.STAY
    assert policy.progress(T0).checks == 2
    decision = policy.observe(check(10))
    assert decision == Decision(
        Action.SWITCH, "cafe", "zte", Reason.BETTER, CAFE.score, CAFE.current_score
    )


def test_a_break_in_the_streak_starts_over() -> None:
    policy = AutoSwitchPolicy()
    policy.observe(check(0))
    policy.observe(check(5))
    policy.observe(check(10, rec=None))  # no longer clearly better
    policy.observe(check(15))
    assert policy.observe(check(20)).action is Action.STAY
    assert policy.observe(check(25)).action is Action.SWITCH


def test_estimated_scores_and_routers_it_cannot_reach_never_count() -> None:
    policy = AutoSwitchPolicy()
    estimated = Recommendation("nb", Score(90, True), Score(61, False), 29)
    for minute in range(0, 30, 5):
        assert policy.observe(check(minute, rec=estimated)).action is Action.STAY
    for minute in range(30, 60, 5):  # measured, but not saved in Windows or not in range
        assert policy.observe(check(minute, candidates={"nb": Score(70, True)})).action is (
            Action.STAY
        )


def test_only_between_your_own_routers() -> None:
    policy = AutoSwitchPolicy()
    for minute in range(0, 30, 5):  # on a network you haven't added, or not connected
        assert policy.observe(check(minute, current=None)).action is Action.STAY


def test_a_router_that_is_down_is_left_after_two_checks() -> None:
    policy = AutoSwitchPolicy()
    down = Verdict.ROUTER_UNREACHABLE
    assert policy.observe(check(0, rec=None, verdict=down)).action is Action.STAY
    decision = policy.observe(check(1, rec=None, verdict=down))
    # Even an estimated score beats no connection; the best one wins.
    assert (decision.action, decision.router_id, decision.reason) == (
        Action.SWITCH,
        "cafe",
        Reason.DOWN,
    )
    assert auto_switch_reason(decision, NAMES) == "ZTE was not responding for 2 checks in a row"


def test_cooldown_after_any_switch() -> None:
    policy = AutoSwitchPolicy()
    policy.switched("zte", "nb", T0, automatic=False)  # your own Switch button
    for minute in range(0, 30, 5):
        assert policy.observe(check(minute)).action is Action.STAY
    assert policy.progress(T0 + timedelta(minutes=18)).cooldown_left == timedelta(minutes=12)
    assert policy.observe(check(30)).action is Action.SWITCH  # the streak built up meanwhile


def test_goes_back_when_the_new_router_fails_and_avoids_it() -> None:
    policy = AutoSwitchPolicy()
    policy.switched("zte", "cafe", T0, automatic=True)
    back = policy.observe(check(1, rec=None, verdict=Verdict.UNSTABLE, current="cafe"))
    assert (back.action, back.router_id, back.from_id) == (Action.BACK, "zte", "cafe")
    message = auto_switch_message(back, NAMES)
    assert message.title == "Back on ZTE" and message.back_to is None
    assert message.text == (
        "Cafe was unstable right after the switch. Cafe won't be picked automatically for 2 hours."
    )
    policy.switched("cafe", "zte", T0 + timedelta(minutes=1), automatic=False)
    # Cafe stays avoided for 2 hours, however good it looks.
    for minute in range(40, 120, 5):
        assert policy.observe(check(minute)).action is Action.STAY
    for minute in (122, 127):
        policy.observe(check(minute))
    assert policy.observe(check(132)).action is Action.SWITCH


def test_a_good_first_check_confirms_the_switch() -> None:
    policy = AutoSwitchPolicy()
    policy.switched("zte", "cafe", T0, automatic=True)
    assert policy.observe(check(1, rec=None, current="cafe")).action is Action.STAY
    down_later = check(6, rec=None, verdict=Verdict.UNSTABLE, current="cafe")
    assert policy.observe(down_later).action is Action.STAY  # only the first check decides


def test_a_failed_connection_avoids_that_router() -> None:
    policy = AutoSwitchPolicy()
    policy.switch_failed("cafe", T0)
    for minute in range(31, 120, 5):
        assert policy.observe(check(minute)).action is Action.STAY


def test_the_last_switch_from_history_keeps_the_pause() -> None:
    policy = AutoSwitchPolicy()
    policy.restore_last_switch(T0 - timedelta(minutes=20))
    for minute in range(0, 10, 3):
        assert policy.observe(check(minute)).action is Action.STAY
    assert policy.observe(check(10)).action is Action.SWITCH


def test_texts() -> None:
    better = Decision(Action.SWITCH, "cafe", "zte", Reason.BETTER, CAFE.score, CAFE.current_score)
    message = auto_switch_message(better, NAMES)
    assert (message.level, message.title, message.back_to) == (
        StatusLevel.GOOD,
        "Switched to Cafe",
        "zte",
    )
    assert message.text == "Cafe was clearly better for 3 checks in a row (score 88 vs 61)."
    policy = AutoSwitchPolicy()
    assert auto_switch_progress(policy.progress(T0), NAMES) is None
    policy.observe(check(0))
    assert auto_switch_progress(policy.progress(T0), NAMES) == (
        "Cafe has been better for 1 of 3 checks."
    )
    policy.switched("zte", "cafe", T0, automatic=False)
    assert auto_switch_progress(policy.progress(T0 + timedelta(minutes=5)), NAMES) == (
        "Automatic switching pauses for 25 more min after the last switch."
    )
