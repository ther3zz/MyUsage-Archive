"""Durable SQLite archive for interval and daily readings.

The archive is the product: the portal only ever shows a rolling seven-day
window of 15-minute data, so anything not captured here is gone. Design
rules (see the implementation plan, §1.7 and §3):

* **Connect per call, never cache a connection.** Callers in Home Assistant
  reach us from an arbitrary executor thread, and a ``sqlite3`` connection is
  bound to the thread that created it. ``check_same_thread`` stays at its
  default of ``True`` so a violation fails loudly.
* **Event-loop guard.** HA's blocking-call detector does not cover
  ``sqlite3``. Every entry point raises :class:`BlockingCallError` if an
  asyncio loop is running on the calling thread. The synchronous CLI passes
  ``allow_event_loop=True``; it has no loop anyway.
* **Exact numbers.** Usage values are stored as decimal *text*, not REAL, so
  the cumulative sums derived later are a deterministic decimal fold rather
  than accumulated float noise.
* **Absent is not zero, and NULL never overwrites a value.** A later fetch
  with an empty cell cannot erase archived data.
* **Nothing changes silently.** Any change to an existing value — including a
  previously-missing value being filled — is written to ``revisions``.
* **Forward-only migrations.** A database newer than this code is refused,
  never "repaired".
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sqlite3
import zlib

# Column names interpolated into SQL below come from hardcoded tuples/dicts in
# this module, never from input; the S608 suppressions are deliberate.
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .exceptions import ArchiveVersionError, BlockingCallError
from .models import DailyRead, IntervalReading, LayoutSignature, Resolution
from .timeutil import EASTERN, day_slots, local_midnight_utc, localize, usage_day

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Portal behaviour that gap math depends on (verified 2026-09-07/08).
PORTAL_LAG_DAYS = 2           # newest interval column is today - 2
PORTAL_WINDOW_DAYS = 7        # seven day columns
INTERVAL_MINUTES = {Resolution.FIFTEEN_MIN: 15, Resolution.HOURLY: 60}

_SCHEMA = """
CREATE TABLE meters (
    id INTEGER PRIMARY KEY,
    meter_number TEXT UNIQUE NOT NULL,
    service_type TEXT NOT NULL DEFAULT 'Electric',
    has_received INTEGER NOT NULL DEFAULT 0,
    first_seen_utc INTEGER NOT NULL,
    last_seen_utc INTEGER NOT NULL
);

CREATE TABLE fetches (
    id INTEGER PRIMARY KEY,
    fetched_at_utc INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ok INTEGER NOT NULL,
    http_status INTEGER,
    window_start_utc INTEGER,
    window_end_utc INTEGER,
    error TEXT,
    raw_page_id INTEGER,
    layout_signature TEXT
);

CREATE TABLE raw_pages (
    id INTEGER PRIMARY KEY,
    fetched_at_utc INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ok INTEGER NOT NULL,
    content BLOB
);
CREATE INDEX idx_raw_prune ON raw_pages (ok, kind, fetched_at_utc);

CREATE TABLE interval_readings (
    meter_id INTEGER NOT NULL REFERENCES meters (id),
    start_utc INTEGER NOT NULL,
    resolution TEXT NOT NULL DEFAULT '15min',
    kwh_delivered TEXT,
    kwh_received TEXT,
    temperature_f TEXT,
    fetch_id INTEGER NOT NULL REFERENCES fetches (id),
    PRIMARY KEY (meter_id, resolution, start_utc)
) WITHOUT ROWID;

CREATE TABLE daily_reads (
    meter_id INTEGER NOT NULL REFERENCES meters (id),
    from_utc INTEGER NOT NULL,
    to_utc INTEGER,
    posted_utc INTEGER,
    usage_date_local TEXT NOT NULL,
    kwh_delivered TEXT,
    kwh_received TEXT,
    kw TEXT,
    meter_reading TEXT,
    read_type TEXT NOT NULL,
    high_f TEXT,
    low_f TEXT,
    fetch_id INTEGER NOT NULL REFERENCES fetches (id),
    PRIMARY KEY (meter_id, from_utc)
);
CREATE INDEX idx_daily_date ON daily_reads (meter_id, usage_date_local);

CREATE TABLE revisions (
    id INTEGER PRIMARY KEY,
    meter_id INTEGER NOT NULL REFERENCES meters (id),
    resolution TEXT NOT NULL,
    start_utc INTEGER NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    fetch_id INTEGER NOT NULL REFERENCES fetches (id),
    changed_at_utc INTEGER NOT NULL
);
CREATE INDEX idx_rev_changed ON revisions (changed_at_utc);
CREATE INDEX idx_rev_meter_start ON revisions (meter_id, resolution, start_utc);

CREATE TABLE exporter_state (
    statistic_id TEXT PRIMARY KEY,
    anchor_start_utc INTEGER,
    anchor_sum TEXT,
    exported_at_utc INTEGER NOT NULL
);
"""



def _migrate_v2_usage_day(conn: sqlite3.Connection) -> None:
    """v1 attributed a daily read to its *From* date; the portal attributes it
    by when the read closed (timeutil.usage_day). Recompute every row."""
    rows = conn.execute("SELECT rowid, from_utc, to_utc FROM daily_reads").fetchall()
    for row in rows:
        end = row["to_utc"] if row["to_utc"] is not None else row["from_utc"]
        day = usage_day(dt.datetime.fromtimestamp(int(end), dt.UTC))
        conn.execute(
            "UPDATE daily_reads SET usage_date_local=? WHERE rowid=?",
            (day.isoformat(), row["rowid"]),
        )


# Ordered, forward-only migrations: version N -> N+1. Version 1 is created by
# _SCHEMA; later entries transform an existing database in place, either as a
# SQL script or as a Python step (for anything SQL cannot compute).
_MIGRATIONS: dict[int, str | Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v2_usage_day,
}


# ----------------------------------------------------------------- results


@dataclass(slots=True)
class StoreResult:
    """What an upsert did. ``preserved`` counts values kept against a NULL."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    preserved: int = 0
    revisions: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


@dataclass(frozen=True, slots=True)
class MeterInfo:
    id: int
    meter_number: str
    service_type: str
    has_received: bool
    first_seen_utc: int
    last_seen_utc: int


@dataclass(frozen=True, slots=True)
class HourlyBucket:
    """One hour of aggregated intervals, UTC-aligned (Eastern hours align)."""

    start_utc: int
    kwh_delivered: Decimal | None
    kwh_received: Decimal | None
    intervals_present: int
    intervals_expected: int
    delivered_missing: int
    received_missing: int

    @property
    def complete(self) -> bool:
        return self.intervals_present == self.intervals_expected and self.delivered_missing == 0

    @property
    def start(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.start_utc, dt.UTC)


@dataclass(frozen=True, slots=True)
class DailyBucket:
    """One local day of the daily Usage History table, summed across its reads.

    Spans local midnight to the next local midnight. Values are the portal's
    own per-day attribution (see :func:`timeutil.usage_day`), which follows
    ~01:30-aligned read windows rather than midnight; that approximation is
    documented and is exactly what the portal's own daily chart shows.
    """

    day: dt.date
    start_utc: int
    end_utc: int
    kwh_delivered: Decimal | None
    kwh_received: Decimal | None
    reads: int
    read_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DayGap:
    day: dt.date
    expected: int
    present: int
    missing_labels: tuple[str, ...]
    recoverable: bool  # still inside the portal's rolling window

    @property
    def missing(self) -> int:
        return self.expected - self.present


@dataclass(frozen=True, slots=True)
class GapReport:
    meter: str
    first_day: dt.date | None
    last_publishable_day: dt.date
    days: tuple[DayGap, ...]

    @property
    def permanent_missing(self) -> int:
        return sum(d.missing for d in self.days if not d.recoverable)

    @property
    def recoverable_missing(self) -> int:
        return sum(d.missing for d in self.days if d.recoverable)

    @property
    def incomplete_days(self) -> tuple[DayGap, ...]:
        return tuple(d for d in self.days if d.missing)


@dataclass(frozen=True, slots=True)
class ConsistencyIssue:
    kind: str          # 'window_mismatch' | 'estimated_day' | 'coverage'
    severity: str      # 'info' | 'warning'
    meter: str
    detail: str
    window_from_utc: int | None = None
    window_to_utc: int | None = None
    expected: Decimal | None = None
    actual: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Revision:
    id: int
    meter: str
    resolution: str
    start_utc: int
    field: str
    old_value: Decimal | str | None
    new_value: Decimal | str | None
    fetch_id: int
    changed_at_utc: int


@dataclass(frozen=True, slots=True)
class RawPage:
    id: int
    fetched_at_utc: int
    kind: str
    ok: bool
    has_content: bool


@dataclass(frozen=True, slots=True)
class ExporterState:
    statistic_id: str
    anchor_start_utc: int | None
    anchor_sum: Decimal | None
    exported_at_utc: int


# ------------------------------------------------------------------ helpers


def _now_utc() -> int:
    return int(dt.datetime.now(dt.UTC).timestamp())


def _dec_to_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _text_to_dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _ts(value: dt.datetime | None) -> int | None:
    return None if value is None else int(value.timestamp())


def _same(old: Decimal | None, new: Decimal | None) -> bool:
    if old is None or new is None:
        return old is None and new is None
    return old == new


@dataclass
class _Change:
    field_name: str
    old: Any
    new: Any


# ------------------------------------------------------------------ archive


class BackupLock:
    """A held write lock. Deliberately ``check_same_thread=False``: the lock is
    taken and released from different executor threads but never used
    concurrently, which the sqlite3 module permits."""

    def __init__(self, path: Path) -> None:
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False
        )
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._conn.close()
            self._conn = None
            raise

    @property
    def held(self) -> bool:
        return self._conn is not None

    def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute("END")
        finally:
            conn.close()


class Archive:
    """SQLite-backed archive. Cheap to construct; each method opens its own connection."""

    def __init__(
        self,
        path: str | Path,
        *,
        allow_event_loop: bool = False,
        keep_ok_raw_pages: int = 0,
    ) -> None:
        self.path = Path(path)
        self._allow_event_loop = allow_event_loop
        self.keep_ok_raw_pages = keep_ok_raw_pages

    # -- connection discipline

    def _guard(self) -> None:
        if self._allow_event_loop:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise BlockingCallError(
            "Archive accessed from a thread running an asyncio event loop. "
            "Run archive calls in a worker thread, e.g. "
            "hass.async_add_executor_job(...)."
        )

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self._guard()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, so PRAGMA journal_mode can run and
        # transactions are explicit (BEGIN IMMEDIATE ... COMMIT).
        conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            # Ride out a backup lock (see lock_for_backup) instead of failing.
            conn.execute("PRAGMA busy_timeout=30000")
            self._ensure_schema(conn)
            if write:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                if write:
                    conn.execute("COMMIT")
            except BaseException:
                if write and conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise ArchiveVersionError(
                f"archive schema is version {version} but this code understands "
                f"up to {SCHEMA_VERSION}; refusing to open (upgrade myusage-archive)"
            )
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            # WAL is persistent; set it once at creation, outside any transaction.
            conn.execute("PRAGMA journal_mode=WAL")
            # executescript() commits any pending transaction before it runs, so
            # the transaction must be part of the script to be atomic.
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{_SCHEMA}\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;"
            )
            _LOGGER.info("created archive %s (schema v%s)", self.path, SCHEMA_VERSION)
            return
        for target in range(version + 1, SCHEMA_VERSION + 1):
            step = _MIGRATIONS.get(target)
            if step is None:
                raise ArchiveVersionError(f"no migration to schema v{target}")
            if isinstance(step, str):
                conn.executescript(
                    f"BEGIN IMMEDIATE;\n{step}\nPRAGMA user_version={target};\nCOMMIT;"
                )
            else:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    step(conn)
                    conn.execute(f"PRAGMA user_version={target}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            _LOGGER.info("migrated archive %s to schema v%s", self.path, target)

    # -- meters

    def _meter_id(
        self, conn: sqlite3.Connection, meter_number: str, *, has_received: bool | None, now: int
    ) -> int:
        row = conn.execute(
            "SELECT id, has_received FROM meters WHERE meter_number=?", (meter_number,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meters (meter_number, has_received, first_seen_utc, last_seen_utc)"
                " VALUES (?, ?, ?, ?)",
                (meter_number, int(bool(has_received)), now, now),
            )
            return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        # has_received only ever turns on: once a meter has shown an export
        # register, a later capture without one is a layout question, not proof
        # the register vanished.
        received = int(row["has_received"]) or int(bool(has_received))
        conn.execute(
            "UPDATE meters SET last_seen_utc=?, has_received=? WHERE id=?",
            (now, received, row["id"]),
        )
        return int(row["id"])

    def meters(self) -> list[MeterInfo]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM meters ORDER BY meter_number").fetchall()
        return [
            MeterInfo(
                id=r["id"],
                meter_number=r["meter_number"],
                service_type=r["service_type"],
                has_received=bool(r["has_received"]),
                first_seen_utc=r["first_seen_utc"],
                last_seen_utc=r["last_seen_utc"],
            )
            for r in rows
        ]

    # -- fetch log + raw pages

    def record_fetch(
        self,
        kind: str,
        *,
        ok: bool,
        http_status: int | None = None,
        error: str | None = None,
        raw_html: str | None = None,
        layout_signature: LayoutSignature | dict[str, Any] | None = None,
        window_start_utc: int | None = None,
        window_end_utc: int | None = None,
        fetched_at_utc: int | None = None,
    ) -> int:
        """Log one portal fetch. Failure pages are always kept; ok pages only
        when ``keep_ok_raw_pages`` is set (then pruned to that many per kind)."""
        now = fetched_at_utc if fetched_at_utc is not None else _now_utc()
        signature = (
            layout_signature.as_dict()
            if isinstance(layout_signature, LayoutSignature)
            else layout_signature
        )
        with self._connect(write=True) as conn:
            raw_page_id: int | None = None
            if raw_html is not None and (not ok or self.keep_ok_raw_pages > 0):
                conn.execute(
                    "INSERT INTO raw_pages (fetched_at_utc, kind, ok, content) VALUES (?, ?, ?, ?)",
                    (now, kind, int(ok), zlib.compress(raw_html.encode("utf-8"))),
                )
                raw_page_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO fetches (fetched_at_utc, kind, ok, http_status, window_start_utc,"
                " window_end_utc, error, raw_page_id, layout_signature)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now,
                    kind,
                    int(ok),
                    http_status,
                    window_start_utc,
                    window_end_utc,
                    error,
                    raw_page_id,
                    json.dumps(signature) if signature is not None else None,
                ),
            )
            fetch_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            if ok and self.keep_ok_raw_pages > 0:
                self._prune_ok_pages(conn, kind)
        return fetch_id

    def _prune_ok_pages(self, conn: sqlite3.Connection, kind: str) -> None:
        """Keep the newest N ok pages per kind; NULL older BLOBs (no dangling refs)."""
        stale = conn.execute(
            "SELECT id FROM raw_pages WHERE ok=1 AND kind=? AND content IS NOT NULL"
            " ORDER BY fetched_at_utc DESC LIMIT -1 OFFSET ?",
            (kind, self.keep_ok_raw_pages),
        ).fetchall()
        if stale:
            conn.executemany(
                "UPDATE raw_pages SET content=NULL WHERE id=?", [(r["id"],) for r in stale]
            )

    def raw_pages(self, *, kind: str | None = None, ok: bool | None = None) -> list[RawPage]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if ok is not None:
            clauses.append("ok=?")
            params.append(int(ok))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""  # noqa: S608
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, fetched_at_utc, kind, ok, content IS NOT NULL AS has_content"  # noqa: S608
                f" FROM raw_pages{where} ORDER BY fetched_at_utc",
                params,
            ).fetchall()
        return [
            RawPage(r["id"], r["fetched_at_utc"], r["kind"], bool(r["ok"]), bool(r["has_content"]))
            for r in rows
        ]

    def raw_page_content(self, page_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT content FROM raw_pages WHERE id=?", (page_id,)).fetchone()
        if row is None or row["content"] is None:
            return None
        return zlib.decompress(row["content"]).decode("utf-8")

    def fetch_for_raw_page(self, page_id: int) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM fetches WHERE raw_page_id=?", (page_id,)).fetchone()
        return None if row is None else int(row["id"])

    def fetch_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fetches ORDER BY fetched_at_utc DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["ok"] = bool(d["ok"])
            if d.get("layout_signature"):
                d["layout_signature"] = json.loads(d["layout_signature"])
            out.append(d)
        return out

    def last_successful_fetch_utc(self, kind: str | None = None) -> int | None:
        with self._connect() as conn:
            if kind is None:
                row = conn.execute(
                    "SELECT MAX(fetched_at_utc) AS t FROM fetches WHERE ok=1"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT MAX(fetched_at_utc) AS t FROM fetches WHERE ok=1 AND kind=?", (kind,)
                ).fetchone()
        return None if row is None or row["t"] is None else int(row["t"])

    # -- storing readings

    def store_intervals(
        self,
        readings: Iterable[IntervalReading],
        *,
        meter: str,
        fetch_id: int,
        has_received: bool | None = None,
    ) -> StoreResult:
        """Idempotent upsert of interval readings with a revision audit trail."""
        result = StoreResult()
        now = _now_utc()
        rows = list(readings)
        if not rows:
            return result
        with self._connect(write=True) as conn:
            meter_id = self._meter_id(conn, meter, has_received=has_received, now=now)
            for reading in rows:
                existing = conn.execute(
                    "SELECT kwh_delivered, kwh_received, temperature_f FROM interval_readings"
                    " WHERE meter_id=? AND resolution=? AND start_utc=?",
                    (meter_id, reading.resolution.value, reading.start_utc),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO interval_readings (meter_id, start_utc, resolution,"
                        " kwh_delivered, kwh_received, temperature_f, fetch_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            meter_id,
                            reading.start_utc,
                            reading.resolution.value,
                            _dec_to_text(reading.kwh_delivered),
                            _dec_to_text(reading.kwh_received),
                            _dec_to_text(reading.temperature_f),
                            fetch_id,
                        ),
                    )
                    result.inserted += 1
                    continue

                changes: list[_Change] = []
                preserved = 0
                updates: dict[str, str | None] = {}
                for column, new in (
                    ("kwh_delivered", reading.kwh_delivered),
                    ("kwh_received", reading.kwh_received),
                    ("temperature_f", reading.temperature_f),
                ):
                    old = _text_to_dec(existing[column])
                    if _same(old, new):
                        continue
                    if new is None:
                        preserved += 1  # never let an empty cell erase data
                        continue
                    changes.append(_Change(column, old, new))
                    updates[column] = _dec_to_text(new)

                result.preserved += preserved
                if not changes:
                    result.unchanged += 1
                    continue

                assignments = ", ".join(f"{col}=?" for col in updates)
                conn.execute(
                    f"UPDATE interval_readings SET {assignments}, fetch_id=?"  # noqa: S608
                    " WHERE meter_id=? AND resolution=? AND start_utc=?",
                    (*updates.values(), fetch_id, meter_id, reading.resolution.value,
                     reading.start_utc),
                )
                for change in changes:
                    conn.execute(
                        "INSERT INTO revisions (meter_id, resolution, start_utc, field,"
                        " old_value, new_value, fetch_id, changed_at_utc)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            meter_id,
                            reading.resolution.value,
                            reading.start_utc,
                            change.field_name,
                            _dec_to_text(change.old),
                            _dec_to_text(change.new),
                            fetch_id,
                            now,
                        ),
                    )
                    _LOGGER.warning(
                        "revision: meter %s %s %s %s: %s -> %s",
                        meter,
                        reading.resolution.value,
                        reading.start.isoformat(),
                        change.field_name,
                        change.old,
                        change.new,
                    )
                result.updated += 1
                result.revisions += len(changes)
        return result

    def store_daily(self, reads: Iterable[DailyRead], *, fetch_id: int) -> StoreResult:
        """Idempotent upsert of daily reads keyed on (meter, from_utc)."""
        result = StoreResult()
        now = _now_utc()
        rows = list(reads)
        if not rows:
            return result
        columns = (
            "to_utc", "posted_utc", "read_type", "kwh_delivered", "kwh_received",
            "kw", "meter_reading", "high_f", "low_f",
        )
        with self._connect(write=True) as conn:
            meter_ids: dict[str, int] = {}
            for read in rows:
                if read.meter not in meter_ids:
                    meter_ids[read.meter] = self._meter_id(
                        conn, read.meter, has_received=read.kwh_received is not None, now=now
                    )
                meter_id = meter_ids[read.meter]
                from_utc = int(read.from_ts.timestamp())
                new_values: dict[str, Any] = {
                    "to_utc": _ts(read.to_ts),
                    "posted_utc": _ts(read.posted_ts),
                    "read_type": read.read_type,
                    "kwh_delivered": _dec_to_text(read.kwh_delivered),
                    "kwh_received": _dec_to_text(read.kwh_received),
                    "kw": _dec_to_text(read.kw),
                    "meter_reading": _dec_to_text(read.meter_reading),
                    "high_f": _dec_to_text(read.high_f),
                    "low_f": _dec_to_text(read.low_f),
                }
                existing = conn.execute(
                    f"SELECT {', '.join(columns)} FROM daily_reads WHERE meter_id=? AND from_utc=?",  # noqa: S608
                    (meter_id, from_utc),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO daily_reads (meter_id, from_utc, usage_date_local,"  # noqa: S608
                        f" {', '.join(columns)}, fetch_id)"  # noqa: S608
                        f" VALUES (?, ?, ?, {', '.join('?' for _ in columns)}, ?)",
                        (
                            meter_id,
                            from_utc,
                            read.usage_date_local.isoformat(),
                            *(new_values[c] for c in columns),
                            fetch_id,
                        ),
                    )
                    result.inserted += 1
                    continue

                changes: list[_Change] = []
                updates: dict[str, Any] = {}
                for column in columns:
                    old, new = existing[column], new_values[column]
                    if column in {"to_utc", "posted_utc", "read_type"}:
                        equal = old == new
                    else:
                        equal = _same(_text_to_dec(old), _text_to_dec(new))
                    if equal:
                        continue
                    if new is None:
                        result.preserved += 1
                        continue
                    changes.append(_Change(column, old, new))
                    updates[column] = new
                if not changes:
                    result.unchanged += 1
                    continue
                assignments = ", ".join(f"{col}=?" for col in updates)
                conn.execute(
                    f"UPDATE daily_reads SET {assignments}, fetch_id=?"  # noqa: S608
                    " WHERE meter_id=? AND from_utc=?",  # noqa: S608
                    (*updates.values(), fetch_id, meter_id, from_utc),
                )
                for change in changes:
                    conn.execute(
                        "INSERT INTO revisions (meter_id, resolution, start_utc, field,"
                        " old_value, new_value, fetch_id, changed_at_utc)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            meter_id, "daily", from_utc, change.field_name,
                            None if change.old is None else str(change.old),
                            None if change.new is None else str(change.new),
                            fetch_id, now,
                        ),
                    )
                    _LOGGER.warning(
                        "revision: meter %s daily %s %s: %s -> %s",
                        read.meter, read.from_ts.isoformat(), change.field_name,
                        change.old, change.new,
                    )
                result.updated += 1
                result.revisions += len(changes)
        return result

    # -- reading back

    def _meter_row(self, conn: sqlite3.Connection, meter: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM meters WHERE meter_number=?", (meter,)
        ).fetchone()
        return row

    def interval_range(
        self, meter: str, resolution: Resolution = Resolution.FIFTEEN_MIN
    ) -> tuple[int, int] | None:
        with self._connect() as conn:
            m = self._meter_row(conn, meter)
            if m is None:
                return None
            row = conn.execute(
                "SELECT MIN(start_utc) AS lo, MAX(start_utc) AS hi FROM interval_readings"
                " WHERE meter_id=? AND resolution=?",
                (m["id"], resolution.value),
            ).fetchone()
        if row is None or row["lo"] is None:
            return None
        return int(row["lo"]), int(row["hi"])

    def intervals(
        self,
        meter: str,
        *,
        start_utc: int | None = None,
        end_utc: int | None = None,
        resolution: Resolution = Resolution.FIFTEEN_MIN,
    ) -> list[IntervalReading]:
        """Readings in [start_utc, end_utc), oldest first."""
        clauses = ["meter_id=?", "resolution=?"]
        with self._connect() as conn:
            m = self._meter_row(conn, meter)
            if m is None:
                return []
            params: list[Any] = [m["id"], resolution.value]
            if start_utc is not None:
                clauses.append("start_utc>=?")
                params.append(start_utc)
            if end_utc is not None:
                clauses.append("start_utc<?")
                params.append(end_utc)
            rows = conn.execute(
                "SELECT start_utc, kwh_delivered, kwh_received, temperature_f"  # noqa: S608
                f" FROM interval_readings WHERE {' AND '.join(clauses)} ORDER BY start_utc",  # noqa: S608
                params,
            ).fetchall()
        return [
            IntervalReading(
                meter=meter,
                start=dt.datetime.fromtimestamp(r["start_utc"], dt.UTC).astimezone(EASTERN),
                resolution=resolution,
                kwh_delivered=_text_to_dec(r["kwh_delivered"]),
                kwh_received=_text_to_dec(r["kwh_received"]),
                temperature_f=_text_to_dec(r["temperature_f"]),
            )
            for r in rows
        ]

    def hourly_series(
        self, meter: str, *, start_utc: int | None = None, end_utc: int | None = None
    ) -> list[HourlyBucket]:
        """Roll 15-minute readings up to UTC-hour buckets as an exact decimal fold.

        Eastern offsets are whole hours, so UTC hours coincide with local
        hours; every hour has exactly four 15-minute slots regardless of DST.
        """
        buckets: dict[int, dict[str, Any]] = {}
        for reading in self.intervals(meter, start_utc=start_utc, end_utc=end_utc):
            hour = reading.start_utc - (reading.start_utc % 3600)
            b = buckets.setdefault(
                hour,
                {"present": 0, "del": None, "rcv": None, "del_missing": 0, "rcv_missing": 0},
            )
            b["present"] += 1
            if reading.kwh_delivered is None:
                b["del_missing"] += 1
            else:
                b["del"] = (b["del"] or Decimal(0)) + reading.kwh_delivered
            if reading.kwh_received is None:
                b["rcv_missing"] += 1
            else:
                b["rcv"] = (b["rcv"] or Decimal(0)) + reading.kwh_received
        return [
            HourlyBucket(
                start_utc=hour,
                kwh_delivered=b["del"],
                kwh_received=b["rcv"],
                intervals_present=b["present"],
                intervals_expected=4,
                delivered_missing=b["del_missing"],
                received_missing=b["rcv_missing"],
            )
            for hour, b in sorted(buckets.items())
        ]

    def daily_reads(
        self, meter: str, *, from_utc: int | None = None, to_utc: int | None = None
    ) -> list[DailyRead]:
        clauses = ["meter_id=?"]
        with self._connect() as conn:
            m = self._meter_row(conn, meter)
            if m is None:
                return []
            params: list[Any] = [m["id"]]
            if from_utc is not None:
                clauses.append("from_utc>=?")
                params.append(from_utc)
            if to_utc is not None:
                clauses.append("from_utc<?")
                params.append(to_utc)
            rows = conn.execute(
                f"SELECT * FROM daily_reads WHERE {' AND '.join(clauses)} ORDER BY from_utc", params  # noqa: S608
            ).fetchall()

        def aware(ts: int | None) -> dt.datetime | None:
            return None if ts is None else dt.datetime.fromtimestamp(ts, dt.UTC).astimezone(EASTERN)

        out = []
        for r in rows:
            from_ts = aware(r["from_utc"])
            assert from_ts is not None
            out.append(
                DailyRead(
                    meter=meter,
                    from_ts=from_ts,
                    to_ts=aware(r["to_utc"]),
                    posted_ts=aware(r["posted_utc"]),
                    read_type=r["read_type"],
                    kwh_delivered=_text_to_dec(r["kwh_delivered"]),
                    kwh_received=_text_to_dec(r["kwh_received"]),
                    kw=_text_to_dec(r["kw"]),
                    meter_reading=_text_to_dec(r["meter_reading"]),
                    high_f=_text_to_dec(r["high_f"]),
                    low_f=_text_to_dec(r["low_f"]),
                )
            )
        return out

    def daily_buckets(self, meter: str) -> list[DailyBucket]:
        """Daily reads grouped by the portal's usage day, oldest first.

        A field is the decimal sum of the day's rows that carry it, or
        ``None`` when no row does — never zero for a blank.
        """
        groups: dict[dt.date, dict[str, Any]] = {}
        for read in self.daily_reads(meter):
            day = read.usage_date_local
            g = groups.setdefault(day, {"del": None, "rcv": None, "n": 0, "types": []})
            g["n"] += 1
            g["types"].append(read.read_type)
            if read.kwh_delivered is not None:
                g["del"] = (g["del"] or Decimal(0)) + read.kwh_delivered
            if read.kwh_received is not None:
                g["rcv"] = (g["rcv"] or Decimal(0)) + read.kwh_received
        return [
            DailyBucket(
                day=day,
                start_utc=local_midnight_utc(day),
                end_utc=local_midnight_utc(day + dt.timedelta(days=1)),
                kwh_delivered=g["del"],
                kwh_received=g["rcv"],
                reads=g["n"],
                read_types=tuple(g["types"]),
            )
            for day, g in sorted(groups.items())
        ]

    # -- change tracking for the exporter

    def revisions_since(self, since_utc: int, *, meter: str | None = None) -> list[Revision]:
        with self._connect() as conn:
            if meter is None:
                rows = conn.execute(
                    "SELECT r.*, m.meter_number FROM revisions r JOIN meters m ON m.id=r.meter_id"
                    " WHERE r.changed_at_utc>? ORDER BY r.changed_at_utc, r.id",
                    (since_utc,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT r.*, m.meter_number FROM revisions r JOIN meters m ON m.id=r.meter_id"
                    " WHERE r.changed_at_utc>? AND m.meter_number=?"
                    " ORDER BY r.changed_at_utc, r.id",
                    (since_utc, meter),
                ).fetchall()
        return [
            Revision(
                id=r["id"], meter=r["meter_number"], resolution=r["resolution"],
                start_utc=r["start_utc"], field=r["field"],
                old_value=r["old_value"], new_value=r["new_value"],
                fetch_id=r["fetch_id"], changed_at_utc=r["changed_at_utc"],
            )
            for r in rows
        ]

    def earliest_interval_change_since(
        self, meter: str, since_utc: int, resolution: Resolution = Resolution.FIFTEEN_MIN
    ) -> int | None:
        """Earliest interval start affected by any write after ``since_utc``.

        Covers both value revisions and rows that arrived late (their fetch
        happened after the watermark). This is what the statistics exporter
        uses to decide where a contiguous re-import must begin.
        """
        with self._connect() as conn:
            m = self._meter_row(conn, meter)
            if m is None:
                return None
            row = conn.execute(
                "SELECT MIN(s) AS s FROM ("
                " SELECT start_utc AS s FROM revisions"
                "  WHERE meter_id=? AND resolution=? AND changed_at_utc>?"
                " UNION ALL"
                " SELECT i.start_utc AS s FROM interval_readings i"
                "  JOIN fetches f ON f.id=i.fetch_id"
                "  WHERE i.meter_id=? AND i.resolution=? AND f.fetched_at_utc>?"
                ")",
                (m["id"], resolution.value, since_utc, m["id"], resolution.value, since_utc),
            ).fetchone()
        return None if row is None or row["s"] is None else int(row["s"])

    def daily_change_days_since(self, meter: str, since_utc: int) -> list[dt.date]:
        """Usage days whose daily rows were written after ``since_utc``.

        Covers value revisions and late-arriving rows alike (a backfill is
        one big late arrival). The exporter intersects this with the days it
        actually represents as midnight buckets.
        """
        with self._connect() as conn:
            m = self._meter_row(conn, meter)
            if m is None:
                return []
            rows = conn.execute(
                "SELECT DISTINCT d.usage_date_local AS day FROM daily_reads d"
                " JOIN fetches f ON f.id=d.fetch_id"
                " WHERE d.meter_id=? AND f.fetched_at_utc>?"
                " UNION"
                " SELECT DISTINCT d.usage_date_local AS day FROM daily_reads d"
                " JOIN revisions r ON r.meter_id=d.meter_id AND r.start_utc=d.from_utc"
                "  AND r.resolution='daily'"
                " WHERE d.meter_id=? AND r.changed_at_utc>?"
                " ORDER BY day",
                (m["id"], since_utc, m["id"], since_utc),
            ).fetchall()
        return [dt.date.fromisoformat(r["day"]) for r in rows]

    def range_fetch_covered_from_utc(self, kind: str) -> int | None:
        """Earliest *requested* window start among successful fetches of ``kind``.

        Range fetches record the window they asked for, not what came back,
        so "has this span been requested before" is answerable even when the
        portal returned less than was asked.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(window_start_utc) AS s FROM fetches WHERE kind=? AND ok=1"
                " AND window_start_utc IS NOT NULL",
                (kind,),
            ).fetchone()
        return None if row is None or row["s"] is None else int(row["s"])

    def exporter_state(self, statistic_id: str) -> ExporterState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM exporter_state WHERE statistic_id=?", (statistic_id,)
            ).fetchone()
        if row is None:
            return None
        return ExporterState(
            statistic_id=row["statistic_id"],
            anchor_start_utc=row["anchor_start_utc"],
            anchor_sum=_text_to_dec(row["anchor_sum"]),
            exported_at_utc=row["exported_at_utc"],
        )

    def set_exporter_state(
        self, statistic_id: str, *, anchor_start_utc: int | None, anchor_sum: Decimal | None
    ) -> None:
        with self._connect(write=True) as conn:
            conn.execute(
                "INSERT INTO exporter_state (statistic_id, anchor_start_utc, anchor_sum,"
                " exported_at_utc) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(statistic_id) DO UPDATE SET"
                " anchor_start_utc=excluded.anchor_start_utc,"
                " anchor_sum=excluded.anchor_sum, exported_at_utc=excluded.exported_at_utc",
                (statistic_id, anchor_start_utc, _dec_to_text(anchor_sum), _now_utc()),
            )

    # -- gaps

    def gaps(
        self,
        meter: str,
        *,
        today: dt.date | None = None,
        resolution: Resolution = Resolution.FIFTEEN_MIN,
    ) -> GapReport:
        """Per-day completeness from the first archived day through the newest
        day the portal could have published (today − lag)."""
        today = today or dt.datetime.now(dt.UTC).astimezone(EASTERN).date()
        last_publishable = today - dt.timedelta(days=PORTAL_LAG_DAYS)
        oldest_visible = today - dt.timedelta(days=PORTAL_LAG_DAYS + PORTAL_WINDOW_DAYS - 1)
        minutes = INTERVAL_MINUTES[resolution]

        rng = self.interval_range(meter, resolution)
        if rng is None:
            return GapReport(
                meter=meter, first_day=None, last_publishable_day=last_publishable, days=()
            )
        present = {r.start_utc for r in self.intervals(meter, resolution=resolution)}
        first_day = dt.datetime.fromtimestamp(rng[0], dt.UTC).astimezone(EASTERN).date()

        days: list[DayGap] = []
        day = first_day
        while day <= last_publishable:
            slots = day_slots(day, minutes)
            missing: list[str] = []
            for minutes_of_day, occurrence in slots:
                start = localize(day, minutes_of_day, second_pass=bool(occurrence))
                if int(start.timestamp()) not in present:
                    hour, minute = divmod(minutes_of_day, 60)
                    missing.append(f"{hour:02d}:{minute:02d}{'*' if occurrence else ''}")
            days.append(
                DayGap(
                    day=day,
                    expected=len(slots),
                    present=len(slots) - len(missing),
                    missing_labels=tuple(missing),
                    recoverable=day >= oldest_visible,
                )
            )
            day += dt.timedelta(days=1)
        return GapReport(
            meter=meter, first_day=first_day, last_publishable_day=last_publishable,
            days=tuple(days),
        )

    # -- consistency

    def consistency_report(
        self, meter: str, *, tolerance_kwh: Decimal = Decimal("1.0")
    ) -> list[ConsistencyIssue]:
        """Cross-check intervals against the daily table, and flag estimated days.

        * Each daily read window is compared with the sum of intervals inside
          it. Runs of ``Failed`` (zero-length placeholder) reads are coalesced
          into the next successful read, because that is where the portal
          rolls their usage. Mismatches on coalesced windows are informational.
        * A day whose intervals are all identical is the portal's documented
          "spread the daily usage evenly" estimation — flagged, never rejected.
        """
        issues: list[ConsistencyIssue] = []
        readings = self.intervals(meter)
        if not readings:
            return issues
        by_start = {r.start_utc: r for r in readings}
        lo, hi = readings[0].start_utc, readings[-1].start_utc + 15 * 60

        # --- daily windows (with Failed-run coalescing)
        reads = self.daily_reads(meter)
        pending_failed: list[DailyRead] = []
        for read in reads:
            if read.is_failed or read.is_zero_length:
                pending_failed.append(read)
                continue
            if read.to_ts is None or read.kwh_delivered is None:
                pending_failed = []
                continue
            window_from = pending_failed[0].from_ts if pending_failed else read.from_ts
            coalesced = bool(pending_failed)
            pending_failed = []
            w_from, w_to = int(window_from.timestamp()), int(read.to_ts.timestamp())
            if w_from < lo or w_to > hi:
                continue  # window not fully inside interval coverage
            expected_slots = (w_to - w_from) // (15 * 60)
            covered = [
                by_start[t] for t in range(w_from - (w_from % 900), w_to, 900) if t in by_start
            ]
            if len(covered) < expected_slots - 1:
                issues.append(
                    ConsistencyIssue(
                        kind="coverage", severity="info", meter=meter,
                        detail=f"only {len(covered)} of ~{expected_slots} intervals present",
                        window_from_utc=w_from, window_to_utc=w_to,
                    )
                )
                continue
            actual = sum(
                (r.kwh_delivered or Decimal(0) for r in covered if w_from <= r.start_utc < w_to),
                Decimal(0),
            )
            expected = read.kwh_delivered
            if abs(actual - expected) > tolerance_kwh:
                issues.append(
                    ConsistencyIssue(
                        kind="window_mismatch",
                        severity="info" if coalesced else "warning",
                        meter=meter,
                        detail=(
                            f"daily kWh {expected} vs interval sum {actual}"
                            + (" (coalesced across failed reads)" if coalesced else "")
                        ),
                        window_from_utc=w_from, window_to_utc=w_to,
                        expected=expected, actual=actual,
                    )
                )

        # --- estimated (uniformly spread) days
        per_day: dict[dt.date, list[Decimal]] = {}
        for r in readings:
            if r.kwh_delivered is not None:
                per_day.setdefault(r.start.date(), []).append(r.kwh_delivered)
        for day, values in sorted(per_day.items()):
            if len(values) >= 90 and len(set(values)) == 1:
                issues.append(
                    ConsistencyIssue(
                        kind="estimated_day", severity="info", meter=meter,
                        detail=f"{day.isoformat()}: all {len(values)} intervals equal {values[0]}"
                        " (portal spread the daily total evenly)",
                    )
                )
        return issues

    # -- maintenance

    def lock_for_backup(self) -> BackupLock:
        """Checkpoint the WAL and hold a write lock until ``release()``.

        Home Assistant's backup tars the database and its write-ahead log one
        after the other; holding a write lock across the backup guarantees a
        consistent pair (this mirrors the recorder's own backup platform).
        """
        self._guard()
        return BackupLock(self.path)

    def integrity_check(self) -> str:
        with self._connect() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0])

    def checkpoint(self) -> None:
        """Flush the write-ahead log into the main file (used before backups)."""
        with self._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
                for table in ("meters", "fetches", "raw_pages", "interval_readings",
                              "daily_reads", "revisions")
            }
            journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return {"path": str(self.path), "schema_version": version, "journal_mode": journal,
                "rows": counts}
