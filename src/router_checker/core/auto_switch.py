"""Automatic switching: when to move to another router, and when to go back.

Rules (each check of the router you're on is one step):

* Better: the same router is recommended (10+ points better, measured by a Test
  all in the last 30 minutes, never only estimated) on ``BETTER_CHECKS`` checks
  in a row.
* Down: your router didn't respond, or had no internet, on ``DOWN_CHECKS`` checks
  in a row. Then the best router you can switch to is picked, even if its score
  is only estimated: anything is better than no connection.
* After any switch (automatic or your own), no automatic switch for ``COOLDOWN``.
* Going back: if the check right after an automatic switch isn't OK, go back to
  the previous router at once, and don't pick the failed one again for
  ``AVOID_AFTER_FAILURE``. The same holds when connecting to it failed.
* Only between your own routers: never away from, or to, a network you haven't
  added, and never while not connected.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from router_checker.core.models import Recommendation, Score, Verdict

BETTER_CHECKS = 3
DOWN_CHECKS = 2
COOLDOWN = timedelta(minutes=30)
AVOID_AFTER_FAILURE = timedelta(hours=2)
DOWN_VERDICTS = frozenset({Verdict.ROUTER_UNREACHABLE, Verdict.INTERNET_DOWN})
FAILED_VERDICTS = DOWN_VERDICTS | {Verdict.UNSTABLE}


@dataclass(frozen=True, slots=True)
class CheckView:
    """What the policy needs from one regular check."""

    timestamp: datetime
    current_id: str | None  # the router you're on; None: not one of yours, or not connected
    verdict: Verdict | None  # of this check; None when nothing was tested
    recommendation: Recommendation | None
    candidates: Mapping[str, Score]  # routers you could switch to right now, and their scores


class Action(StrEnum):
    STAY = "stay"
    SWITCH = "switch"
    BACK = "back"


class Reason(StrEnum):
    BETTER = "better"
    DOWN = "down"
    FAILED_AFTER_SWITCH = "failed after switch"


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    router_id: str | None = None  # where to go
    from_id: str | None = None  # the router you're on
    reason: Reason | None = None
    score: Score | None = None  # of the router to go to
    current_score: Score | None = None
    verdict: Verdict | None = None  # why the current router counts as down or failed


STAY = Decision(Action.STAY)


@dataclass(frozen=True, slots=True)
class Progress:
    """For the dashboard: a candidate building up, or the pause after a switch."""

    router_id: str | None
    checks: int  # of BETTER_CHECKS
    cooldown_left: timedelta | None


class AutoSwitchPolicy:
    """Implements ``SwitchingPolicy``. The controller tells it about every switch."""

    def __init__(self) -> None:
        self._streak_id: str | None = None
        self._streak = 0
        self._down = 0
        self._last_switch: datetime | None = None
        self._pending: tuple[str, str] | None = None  # (from, to): judge the next check
        self._avoid: dict[str, datetime] = {}

    # --- what happened ------------------------------------------------------------

    def switched(self, from_id: str | None, to_id: str, when: datetime, automatic: bool) -> None:
        """A switch worked. Only an automatic one is judged by the next check."""
        self._last_switch = when
        self._pending = (from_id, to_id) if automatic and from_id else None
        self._reset()

    def switch_failed(self, router_id: str, when: datetime) -> None:
        """Connecting to ``router_id`` failed (the app went back)."""
        self._last_switch = when
        self._avoid[router_id] = when
        self._pending = None
        self._reset()

    def restore_last_switch(self, when: datetime) -> None:
        """At start: the last switch in the history, so the pause survives a restart."""
        if self._last_switch is None or when > self._last_switch:
            self._last_switch = when

    def _reset(self) -> None:
        self._streak_id, self._streak, self._down = None, 0, 0

    # --- deciding -------------------------------------------------------------------

    def observe(self, check: CheckView) -> Decision:
        """Take one check into account and say what to do now."""
        now = check.timestamp
        if self._pending is not None:
            from_id, to_id = self._pending
            self._pending = None
            if check.current_id == to_id and check.verdict in FAILED_VERDICTS:
                self._avoid[to_id] = now
                self._reset()
                return Decision(
                    Action.BACK, from_id, to_id, Reason.FAILED_AFTER_SWITCH, verdict=check.verdict
                )

        if check.current_id is None:
            self._reset()
            return STAY
        self._down = self._down + 1 if check.verdict in DOWN_VERDICTS else 0
        rec = check.recommendation
        target = None
        if (
            rec is not None
            and not rec.score.estimated
            and rec.router_id in check.candidates
            and not self._avoided(rec.router_id, now)
        ):
            target = rec.router_id
        if target is not None and target == self._streak_id:
            self._streak += 1
        else:
            self._streak_id, self._streak = target, int(target is not None)

        if self._cooldown_left(now) is not None:
            return STAY
        if self._down >= DOWN_CHECKS:
            options = {
                rid: score
                for rid, score in check.candidates.items()
                if rid != check.current_id and not self._avoided(rid, now)
            }
            if options:
                best = max(
                    options, key=lambda rid: (options[rid].value, not options[rid].estimated)
                )
                return Decision(
                    Action.SWITCH, best, check.current_id, Reason.DOWN, options[best],
                    verdict=check.verdict,
                )  # fmt: skip
        if self._streak >= BETTER_CHECKS and rec is not None:
            return Decision(
                Action.SWITCH, self._streak_id, check.current_id, Reason.BETTER, rec.score,
                rec.current_score,
            )  # fmt: skip
        return STAY

    def progress(self, now: datetime) -> Progress | None:
        cooldown = self._cooldown_left(now)
        if cooldown is None and not self._streak:
            return None
        return Progress(self._streak_id, min(self._streak, BETTER_CHECKS), cooldown)

    def _cooldown_left(self, now: datetime) -> timedelta | None:
        if self._last_switch is None:
            return None
        left = self._last_switch + COOLDOWN - now
        return left if left > timedelta(0) else None

    def _avoided(self, router_id: str, now: datetime) -> bool:
        since = self._avoid.get(router_id)
        return since is not None and now - since < AVOID_AFTER_FAILURE
