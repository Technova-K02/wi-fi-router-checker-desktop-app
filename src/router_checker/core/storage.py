"""SQLite history store (standard library only). Timestamps are stored as
UTC epoch seconds."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from router_checker.core.mac import MacAddress
from router_checker.core.models import (
    Band,
    BssLoad,
    BusyLevel,
    Busyness,
    CheckRecord,
    Event,
    HourlyAggregate,
    InstabilityReason,
    LinkKind,
    ScanEntry,
    ScanObservation,
    Score,
    ScorePoint,
    Verdict,
)

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    router_id TEXT,
    ssid TEXT,
    bssid TEXT,
    verdict TEXT NOT NULL,
    reasons TEXT NOT NULL,
    gateway_loss_pct REAL,
    gateway_avg_ms REAL,
    gateway_p95_ms REAL,
    gateway_jitter_ms REAL,
    gateway_silent INTEGER NOT NULL,
    internet_loss_pct REAL,
    internet_latency_ms REAL,
    internet_jitter_ms REAL,
    dns_ms REAL,
    rssi INTEGER,
    signal_quality INTEGER,
    score REAL,
    link TEXT NOT NULL DEFAULT 'wifi',
    via_vpn INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_checks_router_ts ON checks (router_id, ts);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    router_id TEXT NOT NULL,
    ssid TEXT NOT NULL,
    bssid TEXT NOT NULL,
    rssi INTEGER NOT NULL,
    link_quality INTEGER NOT NULL,
    frequency_mhz INTEGER NOT NULL,
    channel INTEGER,
    band TEXT NOT NULL,
    station_count INTEGER,
    channel_utilization INTEGER,
    same_channel INTEGER NOT NULL,
    busyness REAL NOT NULL,
    busy_level TEXT NOT NULL,
    busy_estimated INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_router_ts ON observations (router_id, ts);

CREATE TABLE IF NOT EXISTS hourly (
    router_id TEXT NOT NULL,
    hour_ts INTEGER NOT NULL,
    busyness_sum REAL NOT NULL DEFAULT 0,
    busyness_count INTEGER NOT NULL DEFAULT 0,
    score_sum REAL NOT NULL DEFAULT 0,
    score_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (router_id, hour_ts)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    router_id TEXT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    router_id TEXT NOT NULL,
    score INTEGER NOT NULL,
    estimated INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_router_ts ON scores (router_id, ts);
"""


def _ts(dt: datetime) -> float:
    return dt.timestamp()


_ADDED_CHECK_COLUMNS = (
    ("link", "TEXT NOT NULL DEFAULT 'wifi'"),
    ("via_vpn", "INTEGER NOT NULL DEFAULT 0"),
)


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def _check_from_row(row: sqlite3.Row) -> CheckRecord:
    return CheckRecord(
        timestamp=_dt(row["ts"]),
        router_id=row["router_id"],
        ssid=row["ssid"],
        bssid=row["bssid"],
        verdict=Verdict[row["verdict"]],
        reasons=tuple(InstabilityReason[x] for x in json.loads(row["reasons"])),
        gateway_loss_pct=row["gateway_loss_pct"],
        gateway_avg_ms=row["gateway_avg_ms"],
        gateway_p95_ms=row["gateway_p95_ms"],
        gateway_jitter_ms=row["gateway_jitter_ms"],
        gateway_silent=bool(row["gateway_silent"]),
        internet_loss_pct=row["internet_loss_pct"],
        internet_latency_ms=row["internet_latency_ms"],
        internet_jitter_ms=row["internet_jitter_ms"],
        dns_ms=row["dns_ms"],
        rssi=row["rssi"],
        signal_quality=row["signal_quality"],
        score=row["score"],
        link=LinkKind(row["link"]),
        via_vpn=bool(row["via_vpn"]),
    )


class SqliteHistoryStore:
    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(_SCHEMA)
            self._add_missing_columns()
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _add_missing_columns(self) -> None:
        """Version 3 added the connection type; older checks were all over Wi-Fi."""
        have = {row["name"] for row in self._db.execute("PRAGMA table_info(checks)")}
        for name, definition in _ADDED_CHECK_COLUMNS:
            if name not in have:
                self._db.execute(f"ALTER TABLE checks ADD COLUMN {name} {definition}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- checks -------------------------------------------------------------

    def add_check(self, r: CheckRecord) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO checks (ts, router_id, ssid, bssid, verdict, reasons,
                    gateway_loss_pct, gateway_avg_ms, gateway_p95_ms, gateway_jitter_ms,
                    gateway_silent, internet_loss_pct, internet_latency_ms, internet_jitter_ms,
                    dns_ms, rssi, signal_quality, score, link, via_vpn)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _ts(r.timestamp), r.router_id, r.ssid, r.bssid, r.verdict.name,
                    json.dumps([x.name for x in r.reasons]),
                    r.gateway_loss_pct, r.gateway_avg_ms, r.gateway_p95_ms, r.gateway_jitter_ms,
                    int(r.gateway_silent), r.internet_loss_pct, r.internet_latency_ms,
                    r.internet_jitter_ms, r.dns_ms, r.rssi, r.signal_quality, r.score,
                    r.link.value, int(r.via_vpn),
                ),
            )  # fmt: skip

    def checks(self, router_id: str, since: datetime) -> list[CheckRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM checks WHERE router_id = ? AND ts >= ? ORDER BY ts",
                (router_id, _ts(since)),
            ).fetchall()
        return [_check_from_row(row) for row in rows]

    def checks_since(self, since: datetime | None = None) -> list[CheckRecord]:
        """Every network's checks, oldest first (``since=None``: all that are kept)."""
        cutoff = _ts(since) if since is not None else float("-inf")
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM checks WHERE ts >= ? ORDER BY ts, id", (cutoff,)
            ).fetchall()
        return [_check_from_row(row) for row in rows]

    def recent_checks(self, limit: int, router_id: str | None = None) -> list[CheckRecord]:
        """Newest first. ``router_id=None`` includes every network, even unknown ones."""
        query = "SELECT * FROM checks"
        params: tuple[object, ...] = ()
        if router_id is not None:
            query += " WHERE router_id = ?"
            params = (router_id,)
        with self._lock:
            rows = self._db.execute(
                query + " ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [_check_from_row(row) for row in rows]

    # --- scores -------------------------------------------------------------

    def add_scores(self, when: datetime, scores: Sequence[tuple[str, Score]]) -> None:
        rows = [(_ts(when), rid, s.value, int(s.estimated)) for rid, s in scores]
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO scores (ts, router_id, score, estimated) VALUES (?, ?, ?, ?)", rows
            )

    def scores(self, router_id: str, since: datetime) -> list[ScorePoint]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, score, estimated FROM scores"
                " WHERE router_id = ? AND ts >= ? ORDER BY ts",
                (router_id, _ts(since)),
            ).fetchall()
        return [ScorePoint(_dt(r["ts"]), r["score"], bool(r["estimated"])) for r in rows]

    # --- scan observations --------------------------------------------------

    def add_observations(self, observations: Sequence[ScanObservation]) -> None:
        rows = []
        for o in observations:
            e = o.entry
            load = e.bss_load
            rows.append(
                (
                    _ts(o.timestamp), o.router_id, e.ssid, str(e.bssid), e.rssi, e.link_quality,
                    e.frequency_mhz, e.channel, e.band.name,
                    load.station_count if load else None,
                    load.channel_utilization if load else None,
                    o.same_channel_count, o.busyness.value, o.busyness.level.name,
                    int(o.busyness.estimated),
                )
            )  # fmt: skip
        with self._lock, self._db:
            self._db.executemany(
                """INSERT INTO observations (ts, router_id, ssid, bssid, rssi, link_quality,
                    frequency_mhz, channel, band, station_count, channel_utilization,
                    same_channel, busyness, busy_level, busy_estimated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def latest_observation(self, router_id: str) -> ScanObservation | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM observations WHERE router_id = ? ORDER BY ts DESC LIMIT 1",
                (router_id,),
            ).fetchone()
        if row is None:
            return None
        load = None
        if row["station_count"] is not None:
            load = BssLoad(row["station_count"], row["channel_utilization"])
        entry = ScanEntry(
            ssid=row["ssid"],
            bssid=MacAddress.parse(row["bssid"]),
            rssi=row["rssi"],
            link_quality=row["link_quality"],
            frequency_mhz=row["frequency_mhz"],
            channel=row["channel"],
            band=Band[row["band"]],
            bss_load=load,
        )
        busy = Busyness(row["busyness"], BusyLevel[row["busy_level"]], bool(row["busy_estimated"]))
        return ScanObservation(_dt(row["ts"]), row["router_id"], entry, row["same_channel"], busy)

    # --- hourly aggregates --------------------------------------------------

    def add_hourly(
        self, router_id: str, when: datetime, busyness: float | None, score: float | None
    ) -> None:
        hour_ts = int(_ts(when)) // 3600 * 3600
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO hourly (router_id, hour_ts, busyness_sum, busyness_count,
                    score_sum, score_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (router_id, hour_ts) DO UPDATE SET
                    busyness_sum = busyness_sum + excluded.busyness_sum,
                    busyness_count = busyness_count + excluded.busyness_count,
                    score_sum = score_sum + excluded.score_sum,
                    score_count = score_count + excluded.score_count""",
                (
                    router_id, hour_ts,
                    busyness or 0.0, int(busyness is not None),
                    score or 0.0, int(score is not None),
                ),
            )  # fmt: skip

    def hourly(self, router_id: str, since: datetime) -> list[HourlyAggregate]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM hourly WHERE router_id = ? AND hour_ts >= ? ORDER BY hour_ts",
                (router_id, int(_ts(since)) // 3600 * 3600),
            ).fetchall()
        return [
            HourlyAggregate(
                router_id=row["router_id"],
                hour_start=_dt(row["hour_ts"]),
                busyness_avg=(
                    row["busyness_sum"] / row["busyness_count"] if row["busyness_count"] else None
                ),
                score_avg=row["score_sum"] / row["score_count"] if row["score_count"] else None,
            )
            for row in rows
        ]

    # --- events -------------------------------------------------------------

    def add_event(self, event: Event) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO events (ts, router_id, kind, message) VALUES (?, ?, ?, ?)",
                (_ts(event.timestamp), event.router_id, event.kind, event.message),
            )

    def events(self, router_id: str | None, limit: int) -> list[Event]:
        query = "SELECT * FROM events"
        params: tuple[object, ...] = ()
        if router_id is not None:
            query += " WHERE router_id = ?"
            params = (router_id,)
        query += " ORDER BY ts DESC, id DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(query, (*params, limit)).fetchall()
        return [Event(_dt(r["ts"]), r["router_id"], r["kind"], r["message"]) for r in rows]

    def last_event(self, kind: str) -> Event | None:
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM events WHERE kind = ? ORDER BY ts DESC, id DESC LIMIT 1", (kind,)
            ).fetchone()
        return None if r is None else Event(_dt(r["ts"]), r["router_id"], r["kind"], r["message"])

    # --- retention ----------------------------------------------------------

    def purge(self, before: datetime) -> None:
        cutoff = _ts(before)
        with self._lock, self._db:
            for table in ("checks", "observations", "events", "scores"):
                self._db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM hourly WHERE hour_ts < ?", (int(cutoff) // 3600 * 3600,))
