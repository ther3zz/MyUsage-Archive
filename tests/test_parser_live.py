"""Parser tests against the real anonymized captures (OUC solar, 2026-09-08).

The golden numbers here come from the live portal. The strongest assertion in
this file is the cross-check: the 15-minute grid and the hourly grid are
independent tables with different shapes, and rolling one up must reproduce
the other exactly. That validates stride detection, column mapping and the
interval-start semantics simultaneously.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from myusage_archive.const import GRID_HOURLY_ID
from myusage_archive.models import Resolution
from myusage_archive.parser import parse_daily_history, parse_interval_grid

FIXTURES = Path(__file__).parent / "fixtures" / "live"
CAPTURE_DATE = dt.date(2026, 9, 8)

EXPECTED_DELIVERED = Decimal("432.294")
EXPECTED_RECEIVED = Decimal("207.642")


def _grid15():
    return parse_interval_grid(
        (FIXTURES / "03-grid15.html").read_text(encoding="utf-8"),
        reference_date=CAPTURE_DATE,
    )


def _hourly():
    return parse_interval_grid(
        (FIXTURES / "04-gridhourly.html").read_text(encoding="utf-8"),
        table_id=GRID_HOURLY_ID,
        reference_date=CAPTURE_DATE,
    )


def test_15min_grid_shape() -> None:
    grid = _grid15()
    sig = grid.signature
    assert sig.day_count == 7
    assert sig.stride == 4
    assert sig.metrics == ("temp_f", "kwh_delivered", "kwh_received", "kw")
    assert sig.data_rows == 96
    assert sig.total_rows == 102  # 4 header + 96 data + 2 summary
    assert sig.excluded_rows == ("Total", "Average")
    assert sig.anomalies == ()
    assert len(grid.readings) == 96 * 7
    assert grid.has_received


def test_15min_dates_are_newest_first_with_correct_years() -> None:
    grid = _grid15()
    assert grid.dates[0] == dt.date(2026, 9, 6)
    assert grid.dates[-1] == dt.date(2026, 8, 31)
    assert list(grid.dates) == sorted(grid.dates, reverse=True)


def test_15min_totals_match_the_portal() -> None:
    grid = _grid15()
    assert sum(r.kwh_delivered or 0 for r in grid.readings) == EXPECTED_DELIVERED
    assert sum(r.kwh_received or 0 for r in grid.readings) == EXPECTED_RECEIVED


def test_hourly_grid_has_no_demand_column() -> None:
    grid = _hourly()
    assert grid.signature.stride == 3
    assert grid.signature.metrics == ("temp_f", "kwh_delivered", "kwh_received")
    assert grid.signature.data_rows == 24
    assert len(grid.readings) == 24 * 7


def test_hourly_rollup_reproduces_the_hourly_grid_exactly() -> None:
    """The decisive cross-check, and the production proof of probe P10.

    If the 15-minute labels were interval ENDS, every hourly bucket would be
    shifted by one quarter hour and this would fail on nearly every hour.
    """
    fifteen, hourly = _grid15(), _hourly()
    rolled: dict[dt.datetime, list[Decimal]] = defaultdict(
        lambda: [Decimal(0), Decimal(0)]
    )
    for reading in fifteen.readings:
        bucket = reading.start.replace(minute=0)
        rolled[bucket][0] += reading.kwh_delivered or 0
        rolled[bucket][1] += reading.kwh_received or 0

    assert len(hourly.readings) == 168
    for reading in hourly.readings:
        got = rolled.get(reading.start)
        assert got is not None, f"no 15-minute data for {reading.start}"
        assert got[0] == (reading.kwh_delivered or 0), reading.start
        assert got[1] == (reading.kwh_received or 0), reading.start


def test_readings_are_eastern_aware_and_ordered_within_a_day() -> None:
    grid = _grid15()
    first = grid.readings[0]
    assert first.start.tzinfo is not None
    assert first.start.utcoffset() == dt.timedelta(hours=-4)  # EDT in September
    assert first.start.hour == 0 and first.start.minute == 0
    assert first.resolution is Resolution.FIFTEEN_MIN
    day_one = [r for r in grid.readings if r.start.date() == dt.date(2026, 9, 6)]
    assert len(day_one) == 96
    assert [r.start for r in day_one] == sorted(r.start for r in day_one)


def test_no_reading_is_silently_zeroed() -> None:
    """Every cell in this capture had a value; none may be None or fabricated."""
    grid = _grid15()
    assert not any(r.is_empty for r in grid.readings)
    assert all(r.kwh_delivered is not None for r in grid.readings)


def test_daily_history_solar_layout() -> None:
    daily = parse_daily_history(
        (FIXTURES / "05-post-electric-25mo.html").read_text(encoding="utf-8")
    )
    assert daily.columns[0] == "Meter"
    assert "kWh Delivered" in daily.columns and "kWh Received" in daily.columns
    assert daily.excluded_rows == ("Total", "Average")
    assert daily.meters == ("MTR001",)
    assert len(daily.reads) == 458
    assert daily.has_received


def test_daily_read_types_are_an_open_vocabulary() -> None:
    """Live data has three types, not the two the prior art assumed."""
    daily = parse_daily_history(
        (FIXTURES / "05-post-electric-25mo.html").read_text(encoding="utf-8")
    )
    types = {r.read_type for r in daily.reads}
    assert types == {"Valid", "Historical", "Failed"}


def test_failed_reads_are_zero_length_placeholders() -> None:
    """Failed rows carry zeros that are placeholders, not measurements."""
    daily = parse_daily_history(
        (FIXTURES / "05-post-electric-25mo.html").read_text(encoding="utf-8")
    )
    failed = [r for r in daily.reads if r.is_failed]
    assert len(failed) == 31
    assert all(r.is_zero_length for r in failed)
    assert all(r.meter_reading == 0 for r in failed)


def test_daily_timestamps_are_not_midnight_aligned() -> None:
    """Read windows run ~01:30 to ~01:30, which the day-attribution must respect."""
    daily = parse_daily_history(
        (FIXTURES / "06-post-electric-60d.html").read_text(encoding="utf-8")
    )
    valid = [r for r in daily.reads if r.read_type == "Valid" and r.to_ts]
    assert valid
    assert not any(r.from_ts.hour == 0 and r.from_ts.minute == 0 for r in valid[:10])
    assert all(r.from_ts.utcoffset() is not None for r in valid)


def test_interval_sums_reconcile_with_daily_read_windows() -> None:
    """Independent consistency check across two different portal surfaces."""
    grid = _grid15()
    daily = parse_daily_history(
        (FIXTURES / "06-post-electric-60d.html").read_text(encoding="utf-8")
    )
    lo = min(r.start for r in grid.readings)
    hi = max(r.start for r in grid.readings)
    compared = 0
    for read in daily.reads:
        if read.to_ts is None or read.from_ts < lo or read.to_ts > hi:
            continue
        if read.kwh_delivered is None:
            continue
        total = sum(
            r.kwh_delivered or 0
            for r in grid.readings
            if read.from_ts <= r.start < read.to_ts
        )
        # The daily table rounds to whole kWh, so agreement is within 1.
        assert abs(total - read.kwh_delivered) < 1, read.from_ts
        compared += 1
    assert compared >= 5


def test_usage_day_attribution_matches_the_portals_own_chart() -> None:
    """The daily page embeds a per-day chart. Summing our rows by usage day
    must reproduce it exactly for every day in every capture — including
    Failed placeholders, 48-hour catch-up reads and the late-evening reads.
    (Verified rule, 2026-09-08: three captures, 545 rows, zero mismatches.)"""
    import re

    checked = 0
    for name in ("02-history-default.html", "06-post-electric-60d.html",
                 "05-post-electric-25mo.html"):
        html = (FIXTURES / name).read_text(encoding="utf-8")
        chart_text = re.search(r'accessibleDescription = "(.*?)";', html).group(1)  # type: ignore[union-attr]
        chart: dict[dt.date, int] = defaultdict(int)
        for kwh, day in re.findall(r"usage amount is kWh(\d+) on (\d\d/\d\d/\d{4})", chart_text):
            chart[dt.datetime.strptime(day, "%m/%d/%Y").date()] += int(kwh)
        ours: dict[dt.date, Decimal] = defaultdict(Decimal)
        for read in parse_daily_history(html).reads:
            assert read.kwh_delivered is not None
            ours[read.usage_date_local] += read.kwh_delivered
        assert {d: int(v) for d, v in ours.items()} == dict(chart), name
        checked += len(chart)
    assert checked == 457 + 59 + 28
