"""Which router to recommend.

Best score among routers whose data is under 30 minutes old. It is shown only
if it beats the current router by at least 10 points (or if there is no
current router with a score).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from router_checker.core.models import Recommendation, Score

MAX_DATA_AGE = timedelta(minutes=30)
MIN_MARGIN = 10


def recommend(
    scores: Mapping[str, tuple[Score, datetime]],
    current_id: str | None,
    now: datetime,
) -> Recommendation | None:
    """``scores`` maps router id to (score, time of the data behind it)."""
    fresh = {rid: s for rid, (s, ts) in scores.items() if now - ts <= MAX_DATA_AGE}
    if not fresh:
        return None
    best_id = max(fresh, key=lambda rid: (fresh[rid].value, not fresh[rid].estimated))
    if best_id == current_id:
        return None
    current = fresh.get(current_id) if current_id else None
    if current is None:
        if current_id is not None:
            return None  # connected, but no fresh score to compare against
        return Recommendation(best_id, fresh[best_id], None, None)
    margin = fresh[best_id].value - current.value
    if margin < MIN_MARGIN:
        return None
    return Recommendation(best_id, fresh[best_id], current, margin)
