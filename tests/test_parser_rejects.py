"""The parser must fail loudly, and must handle layouts this account cannot show.

Each rejection test corresponds to a defect in the prior art
(`dstamen/myusage-ha`) that produced silently wrong numbers rather than an
error. Getting an exception here is the feature.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from conftest import make_daily_history, make_interval_grid

from myusage_archive.const import GRID_HOURLY_ID
from myusage_archive.exceptions import DataError, LayoutError
from myusage_archive.parser import canonical_metric, parse_daily_history, parse_interval_grid

REF = dt.date(2026, 9, 8)
DATES = ["09/06", "09/05"]
WEEKDAYS = ["Sun", "Sat"]
SOLAR = ["°F", "kWh Del", "kWh Rcvd", "kW"]


def _labels(n: int) -> list[str]:
    out = []
    for i in range(n):
        minutes = i * 15
        hour, minute = divmod(minutes, 60)
        suffix = "AM" if hour < 12 else "PM"
        display = hour % 12 or 12
        out.append(f"{display}:{minute:02d} {suffix}")
    return out


def _values(dates: list[str], rows: int, width: int) -> dict[str, list[list[str]]]:
    return {d: [["1.0"] * width for _ in range(rows)] for d in dates}


def _grid(metrics: list[str], rows: int = 96, dates: list[str] | None = None,
          weekdays: list[str] | None = None, **kw) -> str:
    dates = dates or DATES
    return make_interval_grid(
        "grid15MinuteUsage", dates, weekdays or WEEKDAYS[: len(dates)], metrics,
        _labels(rows), _values(dates, rows, len(metrics)), **kw
    )


# --------------------------------------------------------------- header vocabulary


def test_canonical_metric_handles_both_dialects() -> None:
    assert canonical_metric("kWh Delivered") == "kwh_delivered"
    assert canonical_metric("kWh Del") == "kwh_delivered"
    assert canonical_metric("kWh Received") == "kwh_received"
    assert canonical_metric("kWh Rcvd") == "kwh_received"
    assert canonical_metric("°F") == "temp_f"
    assert canonical_metric("kW") == "kw"
    assert canonical_metric("kWh") == "kwh"
    assert canonical_metric("Sparkle Factor") is None


def test_unknown_metric_header_raises() -> None:
    with pytest.raises(LayoutError, match="unrecognized metric header"):
        parse_interval_grid(_grid(["°F", "Flux", "kWh Rcvd", "kW"]), reference_date=REF)


def test_unknown_daily_column_raises() -> None:
    html = make_daily_history([]).replace("<th>Reading</th>", "<th>Mystery</th>")
    with pytest.raises(LayoutError, match="unrecognized column header"):
        parse_daily_history(html)


def test_missing_usage_column_raises() -> None:
    with pytest.raises(LayoutError, match="no usage metric"):
        parse_interval_grid(_grid(["°F", "kW"]), reference_date=REF)


# ------------------------------------------------------------------ stride/shape


def test_stride_not_divisible_raises() -> None:
    """Three metric cells across two day columns cannot be a repeating group."""
    html = _grid(SOLAR)
    html = html.replace("<td>kW</td>", "", 1)  # break the group width
    with pytest.raises(LayoutError, match="not divisible"):
        parse_interval_grid(html, reference_date=REF)


def test_inconsistent_per_day_groups_raise() -> None:
    """Day 0 solar, day 1 non-solar ordering must not be silently accepted."""
    html = make_interval_grid(
        "grid15MinuteUsage", DATES, WEEKDAYS,
        ["°F", "kWh Del", "kWh Rcvd", "kW"], _labels(96), _values(DATES, 96, 4),
    )
    # Swap the second day's group order so the tuples differ.
    html = html.replace(
        "<td>°F</td><td>kWh Del</td><td>kWh Rcvd</td><td>kW</td>"
        "<td>°F</td><td>kWh Del</td><td>kWh Rcvd</td><td>kW</td>",
        "<td>°F</td><td>kWh Del</td><td>kWh Rcvd</td><td>kW</td>"
        "<td>°F</td><td>kWh Rcvd</td><td>kWh Del</td><td>kW</td>",
    )
    with pytest.raises(LayoutError, match="per-day metric groups differ"):
        parse_interval_grid(html, reference_date=REF)


def test_wrong_row_count_raises_outside_dst() -> None:
    with pytest.raises(LayoutError, match="expected 96"):
        parse_interval_grid(_grid(SOLAR, rows=95), reference_date=REF)


def test_missing_table_raises_and_names_what_was_present() -> None:
    with pytest.raises(LayoutError, match="not found"):
        parse_interval_grid("<html><table id='other'></table></html>", reference_date=REF)


# ------------------------------------------------------------------- cell values


def test_garbage_number_raises_with_context() -> None:
    html = _grid(SOLAR)
    html = html.replace('<td data-raw-value="1.0">1.0</td>', "<td>banana</td>", 1)
    with pytest.raises(DataError, match="banana"):
        parse_interval_grid(html, reference_date=REF)


def test_empty_cell_becomes_none_not_zero() -> None:
    """The prior art substituted 0.0 here, corrupting statistics."""
    dates = ["09/06"]
    # Distinct per-metric values so the blanked cell is unambiguous.
    html = make_interval_grid(
        "grid15MinuteUsage", dates, ["Sun"], SOLAR, _labels(96),
        {"09/06": [["70", "2.5", "0.4", "10.0"] for _ in range(96)]},
    )
    html = html.replace('<td data-raw-value="2.5">2.5</td>', "<td></td>", 1)
    grid = parse_interval_grid(html, reference_date=REF)
    missing = [r for r in grid.readings if r.kwh_delivered is None]
    assert len(missing) == 1
    assert missing[0].kwh_received == Decimal("0.4")  # neighbours unaffected
    assert missing[0].is_empty is False  # received is still present


def test_zero_usage_rows_are_kept() -> None:
    """A net-metered site legitimately reports zero; it must not be skipped."""
    dates = ["09/06"]
    html = make_interval_grid(
        "grid15MinuteUsage", dates, ["Sun"], SOLAR, _labels(96),
        {"09/06": [["70", "0.000", "0.000", "0.000"] for _ in range(96)]},
    )
    grid = parse_interval_grid(html, reference_date=REF)
    assert len(grid.readings) == 96
    assert all(r.kwh_delivered == 0 for r in grid.readings)
    assert not any(r.is_empty for r in grid.readings)


def test_fractional_kwh_is_not_truncated() -> None:
    dates = ["09/06"]
    html = make_interval_grid(
        "grid15MinuteUsage", dates, ["Sun"], SOLAR, _labels(96),
        {"09/06": [["70", "0.891", "0.123", "3.564"] for _ in range(96)]},
    )
    grid = parse_interval_grid(html, reference_date=REF)
    assert grid.readings[0].kwh_delivered == Decimal("0.891")
    assert str(grid.readings[0].kwh_delivered) == "0.891"  # exact, not float-ish


# --------------------------------------------------------------- summary rows


def test_summary_rows_are_excluded_from_both_grids() -> None:
    html = _grid(SOLAR, trailing=[("Total", ["", "9", "9", ""] * 2),
                                  ("Average", ["", "1", "1", ""] * 2)])
    grid = parse_interval_grid(html, reference_date=REF)
    assert grid.signature.excluded_rows == ("Total", "Average")
    assert len(grid.readings) == 96 * 2


def test_daily_summary_rows_excluded() -> None:
    rows = [
        {"meter": "MTR001", "from": "09/06/2026 01:36 AM", "to": "09/07/2026 01:33 AM",
         "kwh_del": "56", "kwh_rcvd": "41", "reading": "11449", "type": "Valid"},
        {"meter": "Total", "kwh_del": "56", "kwh_rcvd": "41"},
        {"meter": "Average", "kwh_del": "56", "kwh_rcvd": "41"},
    ]
    daily = parse_daily_history(make_daily_history(rows))
    assert len(daily.reads) == 1
    assert daily.excluded_rows == ("Total", "Average")
    assert daily.meters == ("MTR001",)


# ------------------------------------------------- layouts this account cannot show


def test_non_solar_single_kwh_daily_layout() -> None:
    """Hypothesised 10-column layout; parser is header-driven so it must work."""
    rows = [{"meter": "M9", "high": "90°", "low": "70°",
             "posted": "09/07/2026 10:28 AM", "from": "09/06/2026 01:36 AM",
             "to": "09/07/2026 01:33 AM", "kwh": "56", "reading": "11449",
             "type": "Valid"}]
    daily = parse_daily_history(make_daily_history(rows, solar=False))
    assert len(daily.reads) == 1
    assert daily.reads[0].kwh_delivered == 56
    assert daily.reads[0].kwh_received is None
    assert not daily.has_received


def test_grid_without_temperature_column() -> None:
    """Another utility may omit °F; stride detection must not depend on it."""
    grid = parse_interval_grid(_grid(["kWh Del", "kWh Rcvd"]), reference_date=REF)
    assert grid.signature.stride == 2
    assert grid.signature.metrics == ("kwh_delivered", "kwh_received")
    assert len(grid.readings) == 96 * 2


def test_non_solar_interval_grid_single_kwh() -> None:
    grid = parse_interval_grid(_grid(["°F", "kWh"]), reference_date=REF)
    assert grid.signature.metrics == ("temp_f", "kwh")
    assert grid.readings[0].kwh_delivered == 1
    assert grid.readings[0].kwh_received is None
    assert not grid.has_received


def test_hourly_grid_expects_24_rows() -> None:
    html = make_interval_grid(
        "gridHourlyUsage", DATES, WEEKDAYS, ["°F", "kWh Del", "kWh Rcvd"],
        [
            f"{(h % 12) or 12}:00 {'AM' if h < 12 else 'PM'} - "
            f"{((h + 1) % 12) or 12}:00 {'AM' if (h + 1) % 24 < 12 else 'PM'}"
            for h in range(24)
        ],
        _values(DATES, 24, 3),
    )
    grid = parse_interval_grid(html, table_id=GRID_HOURLY_ID, reference_date=REF)
    assert grid.signature.data_rows == 24
    assert len(grid.readings) == 48


# ----------------------------------------------------------------- year boundary


def test_december_january_boundary() -> None:
    dates = ["01/02", "12/29"]
    html = make_interval_grid(
        "grid15MinuteUsage", dates, ["Sat", "Tue"], SOLAR,
        _labels(96), _values(dates, 96, 4),
    )
    grid = parse_interval_grid(html, reference_date=dt.date(2027, 1, 4))
    assert grid.dates[0] == dt.date(2027, 1, 2)
    assert grid.dates[1] == dt.date(2026, 12, 29)


def test_weekday_mismatch_is_caught() -> None:
    html = _grid(SOLAR, weekdays=["Mon", "Sat"])
    with pytest.raises(DataError, match="weekday mismatch"):
        parse_interval_grid(html, reference_date=REF)
