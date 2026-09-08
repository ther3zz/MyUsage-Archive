"""DST-week parsing — the layouts we cannot observe until Nov 2026 / Mar 2027.

The portal's actual rendering of a 23- or 25-hour day is UNVERIFIED. The grid
is one shared-row table, so a fall-back week's extra hour has to appear
somewhere, and we do not know whether the portal repeats the 1 AM block, adds
rows at the end, or collapses the hour entirely.

The parser is therefore deliberately lenient in a DST week: it records an
anomaly instead of raising, because that week is observable for seven days a
year and a crash loop would cost us the only chance to learn the layout.
These tests pin that leniency down so it cannot silently become sloppiness.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import make_interval_grid

from myusage_archive.exceptions import LayoutError
from myusage_archive.parser import parse_interval_grid

SOLAR = ["°F", "kWh Del", "kWh Rcvd", "kW"]
FALL_BACK = dt.date(2026, 11, 1)


def _label(minutes: int) -> str:
    hour, minute = divmod(minutes, 60)
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def _normal_labels() -> list[str]:
    return [_label(i * 15) for i in range(96)]


def _fall_back_labels() -> list[str]:
    """100 labels with the 1:00-1:45 AM block repeated, as a real 25-hour day."""
    labels = [_label(i * 15) for i in range(4)]          # 12:00-12:45
    labels += [_label(60 + i * 15) for i in range(4)]    # 1:00-1:45 (EDT)
    labels += [_label(60 + i * 15) for i in range(4)]    # 1:00-1:45 again (EST)
    labels += [_label(i * 15) for i in range(8, 96)]     # 2:00 onward
    return labels


def test_fall_back_week_with_repeated_hour_parses() -> None:
    """100 rows, one repeated hour: the expected real-world rendering."""
    dates = ["11/02", "11/01", "10/31"]
    weekdays = ["Mon", "Sun", "Sat"]
    labels = _fall_back_labels()
    assert len(labels) == 100

    values = {}
    for date in dates:
        rows = []
        for index in range(100):
            # Only Nov 1 has the repeated 1 AM block; other days leave it blank.
            is_repeat_slot = 8 <= index < 12
            if is_repeat_slot and date != "11/01":
                rows.append(["", "", "", ""])
            else:
                rows.append(["70", "1.0", "0.5", "4.0"])
        values[date] = rows

    grid = parse_interval_grid(
        make_interval_grid("grid15MinuteUsage", dates, weekdays, SOLAR, labels, values),
        reference_date=dt.date(2026, 11, 3),
    )

    by_day = {}
    for reading in grid.readings:
        by_day.setdefault(reading.start.date(), []).append(reading)

    assert len(by_day[dt.date(2026, 11, 1)]) == 100  # 25-hour day
    assert len(by_day[dt.date(2026, 11, 2)]) == 96
    assert len(by_day[dt.date(2026, 10, 31)]) == 96

    # The repeated hour must resolve to two distinct UTC instants.
    one_ams = [
        r for r in by_day[dt.date(2026, 11, 1)]
        if r.start.hour == 1 and r.start.minute == 0
    ]
    assert len(one_ams) == 2
    assert one_ams[0].start.utcoffset() != one_ams[1].start.utcoffset()
    assert len({r.start.astimezone(dt.UTC) for r in one_ams}) == 2

    # Every instant across the whole grid is unique.
    instants = [r.start.astimezone(dt.UTC) for r in grid.readings]
    assert len(instants) == len(set(instants))

    assert any("repeated hour" in a for a in grid.signature.anomalies)


def test_fall_back_week_collapsed_to_96_rows_is_tolerated() -> None:
    """If the portal collapses the extra hour we must still ingest the week.

    Losing an hour is a gap. Raising would lose seven days of data plus the
    only annual chance to observe this layout.
    """
    dates = ["11/02", "11/01"]
    weekdays = ["Mon", "Sun"]
    values = {d: [["70", "1.0", "0.5", "4.0"] for _ in range(96)] for d in dates}
    grid = parse_interval_grid(
        make_interval_grid(
            "grid15MinuteUsage", dates, weekdays, SOLAR, _normal_labels(), values
        ),
        reference_date=dt.date(2026, 11, 3),
    )
    assert grid.signature.anomalies  # recorded, not raised
    assert any("DST" in a for a in grid.signature.anomalies)
    assert len(grid.readings) > 0


def test_non_dst_week_still_rejects_a_short_table() -> None:
    """Leniency must apply only to DST weeks."""
    dates = ["09/06", "09/05"]
    values = {d: [["70", "1.0", "0.5", "4.0"] for _ in range(95)] for d in dates}
    with pytest.raises(LayoutError, match="expected 96"):
        parse_interval_grid(
            make_interval_grid(
                "grid15MinuteUsage", dates, ["Sun", "Sat"], SOLAR,
                [_label(i * 15) for i in range(95)], values,
            ),
            reference_date=dt.date(2026, 9, 8),
        )


def test_backward_label_step_outside_dst_is_rejected() -> None:
    """A repeated hour in a normal week means the layout changed."""
    dates = ["09/06"]
    labels = _normal_labels()
    labels[10], labels[11] = labels[11], labels[10]  # scramble the ladder
    values = {"09/06": [["70", "1.0", "0.5", "4.0"] for _ in range(96)]}
    with pytest.raises(LayoutError, match="not ordered"):
        parse_interval_grid(
            make_interval_grid(
                "grid15MinuteUsage", dates, ["Sun"], SOLAR, labels, values
            ),
            reference_date=dt.date(2026, 9, 8),
        )


def test_data_in_a_nonexistent_slot_is_flagged_not_silently_kept() -> None:
    """A normal day cannot have a second 1 AM; such a cell must not become data."""
    dates = ["11/02", "11/01"]
    labels = _fall_back_labels()
    # Nov 2 (a normal 24-hour day) wrongly carries data in the repeated slot.
    values = {d: [["70", "1.0", "0.5", "4.0"] for _ in range(100)] for d in dates}
    grid = parse_interval_grid(
        make_interval_grid("grid15MinuteUsage", dates, ["Mon", "Sun"], SOLAR, labels, values),
        reference_date=dt.date(2026, 11, 3),
    )
    nov2 = [r for r in grid.readings if r.start.date() == dt.date(2026, 11, 2)]
    assert len(nov2) == 96  # the impossible slots were not ingested
    assert any("does not exist" in a for a in grid.signature.anomalies)
