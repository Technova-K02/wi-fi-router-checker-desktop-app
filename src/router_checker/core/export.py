"""CSV export of the stored checks (Settings > Export history).

One row per check, oldest first. The local time is written as
"2026-09-30 00:36:16" with the UTC offset in its own column, so spreadsheets
read it as a date. Numbers use a dot as the decimal separator; empty cells
mean "not measured". The caller opens the file with encoding "utf-8-sig": the
byte-order mark tells Excel the file is UTF-8.

Text cells that start with = + - @ (or a tab or carriage return) get a
leading apostrophe, so a spreadsheet shows them as text instead of running
them as a formula. Wi-Fi names are chosen by whoever runs the network.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from datetime import tzinfo
from typing import TextIO

from router_checker.core.models import CheckRecord

HEADER = (
    "Time",
    "UTC offset",
    "Router",
    "Wi-Fi name",
    "Result",
    "Reasons",
    "Gateway average (ms)",
    "Gateway p95 (ms)",
    "Gateway jitter (ms)",
    "Gateway loss (%)",
    "Gateway answers ping",
    "Internet median (ms)",
    "Internet jitter (ms)",
    "Internet loss (%)",
    "DNS lookup (ms)",
    "Signal (dBm)",
    "Signal quality (%)",
    "Score",
)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_text(text: str | None) -> str:
    """Text for a CSV cell that spreadsheets never treat as a formula."""
    if not text:
        return ""
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def _number(value: float | None, decimals: int = 1) -> str:
    return "" if value is None else f"{value:.{decimals}f}"


def check_row(
    record: CheckRecord, router_names: Mapping[str, str], tz: tzinfo | None = None
) -> list[str]:
    local = record.timestamp.astimezone(tz)
    offset = local.strftime("%z")  # "+0200"
    return [
        local.strftime("%Y-%m-%d %H:%M:%S"),
        f"{offset[:3]}:{offset[3:5]}",
        safe_text(router_names.get(record.router_id or "")),
        safe_text(record.ssid),
        record.verdict.value,
        "; ".join(reason.value for reason in record.reasons),
        _number(record.gateway_avg_ms),
        _number(record.gateway_p95_ms),
        _number(record.gateway_jitter_ms),
        _number(record.gateway_loss_pct),
        "no" if record.gateway_silent else "yes",
        _number(record.internet_latency_ms),
        _number(record.internet_jitter_ms),
        _number(record.internet_loss_pct),
        _number(record.dns_ms),
        _number(record.rssi, 0),
        _number(record.signal_quality, 0),
        _number(record.score, 0),
    ]


def write_checks_csv(
    stream: TextIO,
    records: Iterable[CheckRecord],
    router_names: Mapping[str, str],
    tz: tzinfo | None = None,
) -> int:
    """Write the header and one row per record; returns the number of rows."""
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(HEADER)
    count = 0
    for record in records:
        writer.writerow(check_row(record, router_names, tz))
        count += 1
    return count
