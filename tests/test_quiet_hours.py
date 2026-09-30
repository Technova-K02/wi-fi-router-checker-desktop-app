from datetime import UTC, time

import pytest

from router_checker.core.quiet_hours import QuietHours, format_hhmm, parse_hhmm


@pytest.mark.parametrize(
    ("start", "end", "now", "quiet"),
    [
        ("22:00", "07:00", "23:30", True),
        ("22:00", "07:00", "03:00", True),
        ("22:00", "07:00", "22:00", True),
        ("22:00", "07:00", "07:00", False),  # the end is not included
        ("22:00", "07:00", "21:59", False),
        ("22:00", "07:00", "12:00", False),
        ("13:00", "14:30", "13:00", True),
        ("13:00", "14:30", "14:29", True),
        ("13:00", "14:30", "14:30", False),
        ("13:00", "14:30", "12:59", False),
        ("08:00", "08:00", "08:00", False),  # same start and end: never quiet
    ],
)
def test_contains(start: str, end: str, now: str, quiet: bool) -> None:
    hours = QuietHours(True, parse_hhmm(start), parse_hhmm(end))
    assert hours.contains(parse_hhmm(now)) is quiet


def test_off_is_never_quiet() -> None:
    assert not QuietHours(False).contains(time(23, 0))


def test_seconds_and_time_zones_of_the_clock_are_fine() -> None:
    assert QuietHours(True).contains(time(6, 59, 59, tzinfo=UTC))
    assert not QuietHours(True).contains(time(7, 0, 1))


def test_start_and_end_are_whole_minutes() -> None:
    with pytest.raises(ValueError):
        QuietHours(True, time(22, 0, 30))
    with pytest.raises(ValueError):
        QuietHours(True, time(22, 0, tzinfo=UTC))


@pytest.mark.parametrize("text", ["7:30", "24:00", "07:60", "07:30:00", "", "noon"])
def test_parse_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_hhmm(text)


def test_parse_and_format() -> None:
    assert parse_hhmm("07:05") == time(7, 5)
    assert format_hhmm(time(7, 5)) == "07:05"
