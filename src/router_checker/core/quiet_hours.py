"""Quiet hours: no notifications between two local times, every day.

The range may cross midnight: 22:00-07:00 runs from 22:00 in the evening to
07:00 the next morning (07:00 itself is no longer quiet). The same start and
end means no quiet time at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time

DEFAULT_START = time(22, 0)
DEFAULT_END = time(7, 0)

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True, slots=True)
class QuietHours:
    enabled: bool = False
    start: time = DEFAULT_START
    end: time = DEFAULT_END

    def __post_init__(self) -> None:
        for t in (self.start, self.end):
            if t.tzinfo is not None or t.second or t.microsecond:
                raise ValueError("quiet hours are local times in whole minutes")

    def contains(self, local: time) -> bool:
        """True when notifications are muted at the local wall-clock time ``local``."""
        if not self.enabled or self.start == self.end:
            return False
        now = local.replace(tzinfo=None)
        if self.start < self.end:
            return self.start <= now < self.end
        return now >= self.start or now < self.end


def format_hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def parse_hhmm(text: str) -> time:
    """``"07:30"`` -> ``time(7, 30)``. Anything else raises ValueError."""
    match = _HHMM.match(text)
    if match is None:
        raise ValueError(f"expected a time like 07:30, got {text!r}")
    return time(int(match[1]), int(match[2]))
