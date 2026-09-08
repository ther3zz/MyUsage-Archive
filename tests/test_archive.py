"""Archive tests: connection discipline, idempotency, revisions, rollup, gaps, consistency.

Live fixtures drive the numeric checks; synthetic readings drive the edge
cases the live account cannot produce (missing days, failed-read coalescing,
estimated days).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest

from myusage_archive.archive import PORTAL_LAG_DAYS, SCHEMA_VERSION, Archive
from myusage_archive.const import GRID_HOURLY_ID
from myusage_archive.exceptions import ArchiveVersionError, BlockingCallError
from myusage_archive.models import DailyRead, IntervalReading, Resolution
from myusage_archive.parser import parse_daily_history, parse_interval_grid
from myusage_archive.timeutil import localize

FIXTURES = Path(__file__).parent / "fixtures" / "live"
REF = dt.date(2026, 9, 8)
METER = "MTR001"


@cache
def _live15():
    return parse_interval_grid(
        (FIXTURES / "03-grid15.html").read_text(encoding="utf-8"), reference_date=REF
    )


@cache
def _live_hourly():
    return parse_interval_grid(
        (FIXTURES / "04-gridhourly.html").read_text(encoding="utf-8"),
        table_id=GRID_HOURLY_ID,
        reference_date=REF,
    )


@cache
def _live_daily():
    return parse_daily_history((FIXTURES / "06-post-electric-60d.html").read_text(encoding="utf-8"))


def _archive(tmp_path: Path, **kw) -> Archive:
    return Archive(tmp_path / "archive.db", allow_event_loop=True, **kw)


def _loaded(tmp_path: Path, **kw) -> Archive:
    """Archive with the live 15-minute grid stored."""
    a = _archive(tmp_path, **kw)
    fid = a.record_fetch("grid15", ok=True, http_status=200)
    a.store_intervals(_live15().readings, meter=METER, fetch_id=fid, has_received=True)
    return a


def _reading(
    day: dt.date, minutes: int, delivered: str | None, received: str | None = "0"
) -> IntervalReading:
    return IntervalReading(
        meter=METER,
        start=localize(day, minutes),
        resolution=Resolution.FIFTEEN_MIN,
        kwh_delivered=None if delivered is None else Decimal(delivered),
        kwh_received=None if received is None else Decimal(received),
    )


def _full_day(day: dt.date, delivered: str = "0.250") -> list[IntervalReading]:
    return [_reading(day, m, delivered) for m in range(0, 1440, 15)]


# ------------------------------------------------------------ connection rules


async def test_guard_raises_from_inside_an_event_loop(tmp_path: Path) -> None:
    """HA cannot detect a blocking sqlite call; we must."""
    archive = Archive(tmp_path / "a.db")
    with pytest.raises(BlockingCallError, match="async_add_executor_job"):
        archive.meters()


async def test_guard_permits_worker_threads(tmp_path: Path) -> None:
    """The pattern HA uses: run the blocking call off the loop."""
    archive = Archive(tmp_path / "a.db")
    assert await asyncio.to_thread(archive.meters) == []


def test_escape_hatch_for_synchronous_callers(tmp_path: Path) -> None:
    assert _archive(tmp_path).meters() == []


def test_creates_wal_mode_and_schema_version(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    archive.meters()  # triggers creation
    raw = sqlite3.connect(tmp_path / "archive.db")
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        raw.close()


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    """Without the pragma the REFERENCES clauses would be decorative."""
    archive = _archive(tmp_path)
    with pytest.raises(sqlite3.IntegrityError), archive._connect(write=True) as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO interval_readings (meter_id, start_utc, resolution, fetch_id)"
            " VALUES (999, 0, '15min', 999)"
        )


def test_refuses_a_newer_schema(tmp_path: Path) -> None:
    """A downgrade must not silently corrupt a newer archive."""
    archive = _archive(tmp_path)
    archive.meters()
    raw = sqlite3.connect(tmp_path / "archive.db")
    raw.execute("PRAGMA user_version=99")
    raw.commit()
    raw.close()
    with pytest.raises(ArchiveVersionError, match="version 99"):
        archive.meters()


def test_each_call_uses_a_fresh_connection_across_threads(tmp_path: Path) -> None:
    """Connect-per-call means any thread may use the same Archive object."""
    import threading

    archive = _loaded(tmp_path)
    results: list[int] = []

    def work() -> None:
        results.append(len(archive.hourly_series(METER)))

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [168] * 4


# ------------------------------------------------------------------ storing


def test_store_is_idempotent(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    fid = archive.record_fetch("grid15", ok=True)
    first = archive.store_intervals(_live15().readings, meter=METER, fetch_id=fid)
    second = archive.store_intervals(_live15().readings, meter=METER, fetch_id=fid)
    assert (first.inserted, first.updated) == (672, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 672)
    assert second.revisions == 0
    assert archive.revisions_since(0) == []


def test_value_change_is_recorded_as_a_revision(tmp_path: Path) -> None:
    archive = _loaded(tmp_path)
    original = _live15().readings[0]
    corrected = IntervalReading(
        meter=METER, start=original.start, resolution=original.resolution,
        kwh_delivered=original.kwh_delivered + Decimal("0.5"),
        kwh_received=original.kwh_received, temperature_f=original.temperature_f,
    )
    fid = archive.record_fetch("grid15", ok=True)
    result = archive.store_intervals([corrected], meter=METER, fetch_id=fid)
    assert (result.updated, result.revisions) == (1, 1)
    revision = archive.revisions_since(0)[0]
    assert revision.field == "kwh_delivered"
    assert Decimal(revision.old_value) == original.kwh_delivered
    assert Decimal(revision.new_value) == corrected.kwh_delivered
    assert revision.start_utc == original.start_utc
    stored = archive.intervals(METER, start_utc=original.start_utc, end_utc=original.start_utc + 1)
    assert stored[0].kwh_delivered == corrected.kwh_delivered


def test_null_never_overwrites_a_value(tmp_path: Path) -> None:
    """A later fetch with an empty cell must not erase archived data."""
    archive = _loaded(tmp_path)
    original = _live15().readings[0]
    blank = IntervalReading(
        meter=METER, start=original.start, resolution=original.resolution,
        kwh_delivered=None, kwh_received=None, temperature_f=None,
    )
    fid = archive.record_fetch("grid15", ok=True)
    result = archive.store_intervals([blank], meter=METER, fetch_id=fid)
    assert result.updated == 0
    assert result.preserved == 3
    assert result.revisions == 0
    stored = archive.intervals(METER, start_utc=original.start_utc, end_utc=original.start_utc + 1)
    assert stored[0].kwh_delivered == original.kwh_delivered


def test_filling_a_missing_value_is_a_revision(tmp_path: Path) -> None:
    """NULL -> value is a change the exporter must learn about."""
    archive = _archive(tmp_path)
    day = dt.date(2026, 9, 1)
    fid = archive.record_fetch("grid15", ok=True)
    archive.store_intervals([_reading(day, 0, None)], meter=METER, fetch_id=fid)
    fid2 = archive.record_fetch("grid15", ok=True)
    result = archive.store_intervals([_reading(day, 0, "0.5")], meter=METER, fetch_id=fid2)
    assert result.updated == 1
    revision = archive.revisions_since(0)[0]
    assert revision.old_value is None
    assert revision.new_value == "0.5"


def test_values_round_trip_exactly(tmp_path: Path) -> None:
    """Decimal text storage: what went in comes out, digit for digit."""
    archive = _loaded(tmp_path)
    stored = {r.start_utc: r for r in archive.intervals(METER)}
    for original in _live15().readings:
        back = stored[original.start_utc]
        assert back.kwh_delivered == original.kwh_delivered
        assert str(back.kwh_delivered) == str(original.kwh_delivered)
        assert back.kwh_received == original.kwh_received
        assert back.start == original.start


def test_meter_has_received_only_turns_on(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    fid = archive.record_fetch("grid15", ok=True)
    archive.store_intervals(_live15().readings[:4], meter=METER, fetch_id=fid, has_received=True)
    archive.store_intervals(_live15().readings[:4], meter=METER, fetch_id=fid, has_received=False)
    assert archive.meters()[0].has_received is True


def test_daily_store_idempotent_and_keyed_on_from_timestamp(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    fid = archive.record_fetch("daily", ok=True)
    reads = _live_daily().reads
    first = archive.store_daily(reads, fetch_id=fid)
    second = archive.store_daily(reads, fetch_id=fid)
    assert first.inserted == len(reads)
    assert second.unchanged == len(reads)
    back = archive.daily_reads(METER)
    assert len(back) == len(reads)
    assert {r.read_type for r in back} <= {"Valid", "Failed", "Historical"}


# ------------------------------------------------------------------- rollup


def test_hourly_rollup_matches_portal_hourly_grid(tmp_path: Path) -> None:
    """The archive's decimal fold must reproduce the portal's own hourly table."""
    archive = _loaded(tmp_path)
    buckets = {b.start_utc: b for b in archive.hourly_series(METER)}
    assert len(buckets) == 168
    for reading in _live_hourly().readings:
        bucket = buckets[reading.start_utc]
        assert bucket.complete
        assert bucket.kwh_delivered == reading.kwh_delivered
        assert bucket.kwh_received == reading.kwh_received


def test_hourly_bucket_incompleteness_is_visible(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    day = dt.date(2026, 9, 1)
    fid = archive.record_fetch("grid15", ok=True)
    archive.store_intervals(
        [_reading(day, 0, "0.1"), _reading(day, 15, "0.1"), _reading(day, 30, None)],
        meter=METER, fetch_id=fid,
    )
    bucket = archive.hourly_series(METER)[0]
    assert bucket.intervals_present == 3
    assert bucket.delivered_missing == 1
    assert not bucket.complete
    assert bucket.kwh_delivered == Decimal("0.2")


def test_hourly_series_range_is_half_open(tmp_path: Path) -> None:
    archive = _loaded(tmp_path)
    rng = archive.interval_range(METER)
    assert rng is not None
    lo, _ = rng
    buckets = archive.hourly_series(METER, start_utc=lo, end_utc=lo + 3600 * 3)
    assert len(buckets) == 3


# --------------------------------------------------------------------- gaps


def test_no_gaps_on_a_complete_window(tmp_path: Path) -> None:
    report = _loaded(tmp_path).gaps(METER, today=REF)
    assert report.first_day == dt.date(2026, 8, 31)
    assert report.last_publishable_day == REF - dt.timedelta(days=PORTAL_LAG_DAYS)
    assert len(report.days) == 7
    assert report.permanent_missing == 0
    assert report.recoverable_missing == 0
    assert report.incomplete_days == ()


def test_missing_day_is_reported_and_recoverable_inside_window(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    fid = archive.record_fetch("grid15", ok=True)
    keep = [r for r in _live15().readings if r.start.date() != dt.date(2026, 9, 3)]
    archive.store_intervals(keep, meter=METER, fetch_id=fid)
    report = archive.gaps(METER, today=REF)
    gap_days = {d.day: d for d in report.incomplete_days}
    assert set(gap_days) == {dt.date(2026, 9, 3)}
    gap = gap_days[dt.date(2026, 9, 3)]
    assert gap.missing == 96
    assert gap.recoverable  # still in the 7-day window on Sep 8
    assert report.recoverable_missing == 96
    assert report.permanent_missing == 0


def test_missing_day_becomes_permanent_once_the_window_passes(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    fid = archive.record_fetch("grid15", ok=True)
    keep = [r for r in _live15().readings if r.start.date() != dt.date(2026, 9, 3)]
    archive.store_intervals(keep, meter=METER, fetch_id=fid)
    later = REF + dt.timedelta(days=30)
    report = archive.gaps(METER, today=later)
    sep3 = next(d for d in report.days if d.day == dt.date(2026, 9, 3))
    assert not sep3.recoverable
    assert report.permanent_missing >= 96


def test_partial_day_lists_missing_slots(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    fid = archive.record_fetch("grid15", ok=True)
    day = dt.date(2026, 9, 1)
    readings = [r for r in _full_day(day) if r.start.minute != 30]  # drop every :30
    archive.store_intervals(readings, meter=METER, fetch_id=fid)
    report = archive.gaps(METER, today=dt.date(2026, 9, 3))
    gap = report.incomplete_days[0]
    assert gap.missing == 24
    assert all(label.endswith(":30") for label in gap.missing_labels)


def test_gaps_span_a_fall_back_day_with_100_slots(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    fid = archive.record_fetch("grid15", ok=True)
    archive.store_intervals(_full_day(dt.date(2026, 11, 1)), meter=METER, fetch_id=fid)
    report = archive.gaps(METER, today=dt.date(2026, 11, 3))
    nov1 = report.days[0]
    assert nov1.expected == 100
    assert nov1.missing == 4  # the repeated hour was not stored
    assert all(label.endswith("*") for label in nov1.missing_labels)


# -------------------------------------------------------------- consistency


def test_consistency_clean_on_live_data_at_daily_rounding_tolerance(tmp_path: Path) -> None:
    archive = _loaded(tmp_path)
    fid = archive.record_fetch("daily", ok=True)
    archive.store_daily(_live_daily().reads, fetch_id=fid)
    issues = archive.consistency_report(METER)
    assert [i for i in issues if i.severity == "warning"] == []


def test_consistency_detects_window_mismatch(tmp_path: Path) -> None:
    """Tightening the tolerance below the daily table's rounding exposes the
    real sub-kWh differences, proving the windows are actually compared."""
    archive = _loaded(tmp_path)
    fid = archive.record_fetch("daily", ok=True)
    archive.store_daily(_live_daily().reads, fetch_id=fid)
    issues = archive.consistency_report(METER, tolerance_kwh=Decimal("0.1"))
    mismatches = [i for i in issues if i.kind == "window_mismatch"]
    assert len(mismatches) >= 5
    assert all(i.severity == "warning" for i in mismatches)
    assert all(i.expected is not None and i.actual is not None for i in mismatches)


def _daily(day: dt.date, hour: int, minute: int, to: tuple[dt.date, int, int] | None,
           kwh: str, read_type: str = "Valid") -> DailyRead:
    from_ts = localize(day, hour * 60 + minute)
    to_ts = None if to is None else localize(to[0], to[1] * 60 + to[2])
    return DailyRead(
        meter=METER, from_ts=from_ts, to_ts=to_ts, posted_ts=None, read_type=read_type,
        kwh_delivered=Decimal(kwh), kwh_received=Decimal(0),
        meter_reading=Decimal(0) if read_type == "Failed" else Decimal(100),
    )


def test_failed_run_is_coalesced_into_next_valid_read(tmp_path: Path) -> None:
    """A zero-length Failed read rolls its usage into the next Valid read,
    so the comparison window must start at the Failed read."""
    archive = _archive(tmp_path)
    d1, d2, d3 = dt.date(2026, 9, 1), dt.date(2026, 9, 2), dt.date(2026, 9, 3)
    fid = archive.record_fetch("grid15", ok=True)
    intervals = _full_day(d1, "0.500") + _full_day(d2, "0.500") + _full_day(d3, "0.500")
    archive.store_intervals(intervals, meter=METER, fetch_id=fid)
    # Failed at d2 01:30 (zero-length, kWh 0), then Valid d1 01:30 -> d3 01:30
    # carrying two days of usage: 96 * 0.5 * 2 = 96 kWh.
    reads = [
        _daily(d2, 1, 30, (d2, 1, 30), "0", read_type="Failed"),
        _daily(d2, 1, 30, (d3, 1, 30), "96"),  # the roll-up read
    ]
    # The Valid read's own window is only one day, but with the Failed read
    # coalesced in front the window spans two days and matches.
    reads[0] = _daily(d1, 1, 30, (d1, 1, 30), "0", read_type="Failed")
    fid2 = archive.record_fetch("daily", ok=True)
    archive.store_daily(reads, fetch_id=fid2)
    issues = archive.consistency_report(METER)
    assert [i for i in issues if i.kind == "window_mismatch"] == []


def test_failed_window_mismatch_is_informational(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    d1, d2, d3 = dt.date(2026, 9, 1), dt.date(2026, 9, 2), dt.date(2026, 9, 3)
    fid = archive.record_fetch("grid15", ok=True)
    archive.store_intervals(
        _full_day(d1, "0.500") + _full_day(d2, "0.500") + _full_day(d3, "0.500"),
        meter=METER, fetch_id=fid,
    )
    reads = [
        _daily(d1, 1, 30, (d1, 1, 30), "0", read_type="Failed"),
        _daily(d2, 1, 30, (d3, 1, 30), "50"),  # wrong: intervals say 96
    ]
    fid2 = archive.record_fetch("daily", ok=True)
    archive.store_daily(reads, fetch_id=fid2)
    issues = [i for i in archive.consistency_report(METER) if i.kind == "window_mismatch"]
    assert len(issues) == 1
    assert issues[0].severity == "info"
    assert "coalesced" in issues[0].detail


def test_estimated_day_is_flagged_not_rejected(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    fid = archive.record_fetch("grid15", ok=True)
    archive.store_intervals(_full_day(dt.date(2026, 9, 1), "0.625"), meter=METER, fetch_id=fid)
    issues = archive.consistency_report(METER)
    assert [i.kind for i in issues] == ["estimated_day"]
    assert issues[0].severity == "info"
    assert "0.625" in issues[0].detail


# ----------------------------------------------------- fetch log + raw pages


def test_failure_pages_are_always_kept(tmp_path: Path) -> None:
    archive = _archive(tmp_path, keep_ok_raw_pages=0)
    archive.record_fetch("grid15", ok=True, raw_html="<ok/>")
    fid = archive.record_fetch("grid15", ok=False, error="LayoutError: x", raw_html="<bad/>")
    pages = archive.raw_pages()
    assert [(p.ok, p.has_content) for p in pages] == [(False, True)]
    assert archive.raw_page_content(pages[0].id) == "<bad/>"
    assert archive.fetch_for_raw_page(pages[0].id) == fid


def test_ok_pages_are_pruned_to_the_configured_count(tmp_path: Path) -> None:
    archive = _archive(tmp_path, keep_ok_raw_pages=2)
    for i in range(4):
        archive.record_fetch("grid15", ok=True, raw_html=f"<p{i}/>", fetched_at_utc=1000 + i)
    pages = archive.raw_pages(ok=True)
    assert len(pages) == 4  # rows stay (no dangling fetch refs) ...
    assert [p.has_content for p in pages] == [False, False, True, True]  # ... BLOBs pruned


def test_layout_signature_is_stored_as_json(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    archive.record_fetch("grid15", ok=True, layout_signature=_live15().signature)
    history = archive.fetch_history()
    assert history[0]["layout_signature"]["stride"] == 4
    assert history[0]["layout_signature"]["metrics"] == [
        "temp_f", "kwh_delivered", "kwh_received", "kw",
    ]


def test_last_successful_fetch(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    assert archive.last_successful_fetch_utc() is None
    archive.record_fetch("grid15", ok=False, fetched_at_utc=50)
    archive.record_fetch("grid15", ok=True, fetched_at_utc=100)
    archive.record_fetch("daily", ok=True, fetched_at_utc=200)
    assert archive.last_successful_fetch_utc() == 200
    assert archive.last_successful_fetch_utc("grid15") == 100


# ------------------------------------------------------- exporter support


def test_exporter_state_round_trip(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    assert archive.exporter_state("myusage_archive:x") is None
    archive.set_exporter_state(
        "myusage_archive:x", anchor_start_utc=1000, anchor_sum=Decimal("12.345")
    )
    state = archive.exporter_state("myusage_archive:x")
    assert state is not None
    assert state.anchor_start_utc == 1000
    assert state.anchor_sum == Decimal("12.345")
    archive.set_exporter_state("myusage_archive:x", anchor_start_utc=2000, anchor_sum=Decimal("20"))
    assert archive.exporter_state("myusage_archive:x").anchor_start_utc == 2000  # type: ignore[union-attr]


def test_earliest_change_since_sees_revisions_and_late_rows(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    day = dt.date(2026, 9, 1)
    fid = archive.record_fetch("grid15", ok=True, fetched_at_utc=1000)
    archive.store_intervals(
        [_reading(day, 60, "1"), _reading(day, 75, "1")], meter=METER, fetch_id=fid
    )
    # Nothing has changed since after that fetch.
    assert archive.earliest_interval_change_since(METER, since_utc=10**10) is None
    # A late-arriving earlier row (fetched later, but earlier in time).
    fid2 = archive.record_fetch("grid15", ok=True, fetched_at_utc=2000)
    archive.store_intervals([_reading(day, 0, "1")], meter=METER, fetch_id=fid2)
    assert archive.earliest_interval_change_since(METER, since_utc=1500) == int(
        localize(day, 0).timestamp()
    )
    # A revision to an existing row.
    fid3 = archive.record_fetch("grid15", ok=True, fetched_at_utc=3000)
    archive.store_intervals([_reading(day, 75, "2")], meter=METER, fetch_id=fid3)
    assert archive.earliest_interval_change_since(METER, since_utc=2500) == int(
        localize(day, 75).timestamp()
    )


# ------------------------------------------------------------- maintenance


def test_integrity_and_stats(tmp_path: Path) -> None:
    archive = _loaded(tmp_path)
    assert archive.integrity_check() == "ok"
    stats = archive.stats()
    assert stats["journal_mode"] == "wal"
    assert stats["rows"]["interval_readings"] == 672
    archive.checkpoint()  # must not raise


# ------------------------------------------------------- daily buckets (M3)


@cache
def _live_daily_15mo():
    return parse_daily_history(
        (FIXTURES / "05-post-electric-25mo.html").read_text(encoding="utf-8")
    )


def _daily_loaded(tmp_path: Path, reads=None) -> Archive:
    a = _archive(tmp_path)
    fid = a.record_fetch("daily_range", ok=True, http_status=200)
    a.store_daily(reads if reads is not None else _live_daily_15mo().reads, fetch_id=fid)
    return a


def test_daily_buckets_follow_the_portals_day_attribution(tmp_path: Path) -> None:
    buckets = {b.day: b for b in _daily_loaded(tmp_path).daily_buckets(METER)}
    # Failed placeholder (read 08/14 01:32) -> Aug 13: delivered 0, received 23 verbatim.
    aug13 = buckets[dt.date(2026, 8, 13)]
    assert (aug13.kwh_delivered, aug13.kwh_received) == (Decimal(0), Decimal(23))
    assert buckets[dt.date(2026, 8, 13)].read_types == ("Failed",)
    # The 48 h catch-up read that follows lands on Aug 14 with the rolled-up usage.
    assert buckets[dt.date(2026, 8, 14)].kwh_delivered == Decimal(135)
    # Two reads closing on the same day (a 44 h Historical + a 3.8 h Valid) are summed.
    sep10 = buckets[dt.date(2025, 9, 10)]
    assert sep10.reads == 2 and sep10.kwh_delivered == Decimal(99 + 8)
    assert sep10.read_types == ("Historical", "Valid")
    # Spans local midnight to local midnight, ordered oldest first.
    days = list(buckets)
    assert days == sorted(days)
    for b in buckets.values():
        assert b.end_utc - b.start_utc in (23 * 3600, 24 * 3600, 25 * 3600)
        assert b.start_utc % 3600 == 0


def test_daily_bucket_blank_field_stays_none(tmp_path: Path) -> None:
    day = dt.date(2026, 5, 5)
    reads = [
        DailyRead(meter=METER, from_ts=localize(day, 90),
                  to_ts=localize(day + dt.timedelta(days=1), 95),
                  posted_ts=None, read_type="Valid", kwh_delivered=None, kwh_received=None),
    ]
    (bucket,) = _daily_loaded(tmp_path, reads).daily_buckets(METER)
    assert bucket.day == day and bucket.kwh_delivered is None and bucket.kwh_received is None


def test_migration_v1_to_v2_recomputes_usage_day(tmp_path: Path) -> None:
    """A v1 archive attributed reads to their From date. Opening it with v2
    code must rewrite every row to the portal's rule, atomically, once."""
    archive = _daily_loaded(tmp_path)
    raw = sqlite3.connect(tmp_path / "archive.db")
    try:
        # Forge the v1 state: From-date attribution and user_version=1.
        raw.execute("UPDATE daily_reads SET usage_date_local = date(from_utc, 'unixepoch')")
        raw.execute("PRAGMA user_version=1")
        raw.commit()
        forged = raw.execute(
            "SELECT usage_date_local FROM daily_reads WHERE from_utc=?",
            (int(localize(dt.date(2026, 8, 13), 92).timestamp()),),
        ).fetchone()[0]
        assert forged == "2026-08-13"
    finally:
        raw.close()

    buckets = {b.day: b for b in archive.daily_buckets(METER)}  # triggers the migration
    assert buckets[dt.date(2026, 8, 14)].kwh_delivered == Decimal(135)
    assert dt.date(2025, 9, 10) in buckets and buckets[dt.date(2025, 9, 10)].reads == 2
    raw = sqlite3.connect(tmp_path / "archive.db")
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert raw.execute(
            "SELECT COUNT(*) FROM daily_reads WHERE usage_date_local NOT LIKE '____-__-__'"
        ).fetchone()[0] == 0
    finally:
        raw.close()


def test_daily_change_days_since_sees_late_rows_and_revisions(tmp_path: Path) -> None:
    import time

    archive = _archive(tmp_path)
    t_before = int(time.time()) - 5
    fid = archive.record_fetch("daily", ok=True)
    archive.store_daily(_live_daily().reads, fetch_id=fid)
    late = archive.daily_change_days_since(METER, t_before)
    assert late and late == sorted(late)
    assert dt.date(2026, 8, 13) in late and dt.date(2026, 8, 14) in late
    # Nothing after "now": no late rows.
    assert archive.daily_change_days_since(METER, int(time.time()) + 60) == []

    # A revision to one row shows up under its usage day, even with an old fetch.
    target = next(r for r in _live_daily().reads if r.usage_date_local == dt.date(2026, 8, 14))
    corrected = DailyRead(
        meter=METER, from_ts=target.from_ts, to_ts=target.to_ts, posted_ts=target.posted_ts,
        read_type=target.read_type, kwh_delivered=target.kwh_delivered + Decimal(1),
        kwh_received=target.kwh_received, kw=target.kw, meter_reading=target.meter_reading,
        high_f=target.high_f, low_f=target.low_f,
    )
    import myusage_archive.archive as archive_module

    original_now = archive_module._now_utc
    archive_module._now_utc = lambda: original_now() + 600  # noqa: SLF001
    try:
        fid2 = archive.record_fetch("daily", ok=True)
        assert archive.store_daily([corrected], fetch_id=fid2).revisions == 1
    finally:
        archive_module._now_utc = original_now
    changed = archive.daily_change_days_since(METER, int(time.time()) + 60)
    assert changed == [dt.date(2026, 8, 14)]


def test_range_fetch_covered_from_utc_uses_requested_window(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    assert archive.range_fetch_covered_from_utc("daily_range") is None
    archive.record_fetch("daily_range", ok=False, window_start_utc=100)   # failures don't count
    archive.record_fetch("daily_range", ok=True, window_start_utc=500)
    archive.record_fetch("daily_range", ok=True, window_start_utc=300)
    archive.record_fetch("daily", ok=True, window_start_utc=1)            # other kinds don't count
    assert archive.range_fetch_covered_from_utc("daily_range") == 300
