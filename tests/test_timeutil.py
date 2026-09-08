"""Eastern-time handling, including the DST cases we cannot observe until Nov 2026."""

from __future__ import annotations

import datetime as dt

import pytest

from myusage_archive.exceptions import DataError
from myusage_archive.timeutil import (
    EASTERN,
    day_length_minutes,
    day_slots,
    eastern_today,
    expected_interval_count,
    has_dst_transition,
    infer_year,
    is_time_label,
    localize,
    parse_local_datetime,
    parse_mmdd,
    parse_time_label,
    slot_exists,
)

FALL_BACK = dt.date(2026, 11, 1)   # 25 hours
SPRING_FWD = dt.date(2027, 3, 14)  # 23 hours
NORMAL = dt.date(2026, 9, 6)


def test_time_labels_both_dialects() -> None:
    assert parse_time_label("12:00 AM") == 0
    assert parse_time_label("12:15 AM") == 15
    assert parse_time_label("1:00 AM") == 60
    assert parse_time_label("12:00 PM") == 720
    assert parse_time_label("11:45 PM") == 1425
    # Hourly grid renders a range; the label is the START.
    assert parse_time_label("12:00 AM - 01:00 AM") == 0
    assert parse_time_label("01:00 AM - 02:00 AM") == 60


def test_summary_labels_are_not_times() -> None:
    for label in ("Total", "Average", "", "Peak Demand"):
        assert not is_time_label(label)
    with pytest.raises(DataError):
        parse_time_label("Total")


def test_day_lengths_across_dst() -> None:
    assert day_length_minutes(NORMAL) == 1440
    assert day_length_minutes(FALL_BACK) == 1500  # 25 hours
    assert day_length_minutes(SPRING_FWD) == 1380  # 23 hours
    assert expected_interval_count(NORMAL, 15) == 96
    assert expected_interval_count(FALL_BACK, 15) == 100
    assert expected_interval_count(SPRING_FWD, 15) == 92
    assert expected_interval_count(FALL_BACK, 60) == 25


def test_has_dst_transition() -> None:
    assert has_dst_transition([NORMAL, FALL_BACK])
    assert not has_dst_transition([NORMAL, dt.date(2026, 9, 5)])


def test_day_slots_counts_match_day_length() -> None:
    assert len(day_slots(NORMAL, 15)) == 96
    assert len(day_slots(FALL_BACK, 15)) == 100
    assert len(day_slots(SPRING_FWD, 15)) == 92


def test_fall_back_day_yields_100_distinct_instants_15_min_apart() -> None:
    instants = sorted(
        localize(FALL_BACK, minutes, second_pass=bool(occurrence)).astimezone(dt.UTC)
        for minutes, occurrence in day_slots(FALL_BACK, 15)
    )
    assert len(set(instants)) == 100
    deltas = {(b - a).total_seconds() for a, b in zip(instants, instants[1:], strict=False)}
    assert deltas == {900.0}


def test_localize_uses_wall_clock_not_elapsed_time() -> None:
    """The regression that shifted every post-transition row an hour early.

    On a fall-back day, "2:00 AM" is 2:00 AM EST — not midnight plus 120
    minutes of real time, which would land at 1:00 AM EST.
    """
    two_am = localize(FALL_BACK, 120)
    assert (two_am.hour, two_am.minute) == (2, 0)
    assert two_am.utcoffset() == dt.timedelta(hours=-5)  # EST
    first_one_am = localize(FALL_BACK, 60, second_pass=False)
    second_one_am = localize(FALL_BACK, 60, second_pass=True)
    assert two_am.astimezone(dt.UTC) > second_one_am.astimezone(dt.UTC)
    assert second_one_am.astimezone(dt.UTC) > first_one_am.astimezone(dt.UTC)


def test_localize_rejects_out_of_range_minutes() -> None:
    with pytest.raises(DataError):
        localize(NORMAL, 1440)


def test_localize_second_pass_selects_repeated_hour() -> None:
    first = localize(FALL_BACK, 60, second_pass=False)   # 1:00 AM EDT
    second = localize(FALL_BACK, 60, second_pass=True)
    assert first.utcoffset() != second.utcoffset()


def test_slot_exists_normal_day() -> None:
    assert slot_exists(NORMAL, 0)
    assert slot_exists(NORMAL, 1425)
    assert not slot_exists(NORMAL, 60, occurrence=1)  # no repeated hour
    assert not slot_exists(NORMAL, 1440)  # past midnight


def test_slot_exists_fall_back_has_repeat() -> None:
    assert slot_exists(FALL_BACK, 60, occurrence=0)
    assert slot_exists(FALL_BACK, 60, occurrence=1)
    assert slot_exists(FALL_BACK, 75, occurrence=1)
    assert not slot_exists(FALL_BACK, 180, occurrence=1)  # 3 AM is not ambiguous


def test_slot_exists_spring_forward_skips_hour() -> None:
    assert slot_exists(SPRING_FWD, 60)       # 1:00 AM exists
    assert not slot_exists(SPRING_FWD, 120)  # 2:00 AM does not exist
    assert not slot_exists(SPRING_FWD, 165)  # 2:45 AM does not exist
    assert slot_exists(SPRING_FWD, 180)      # 3:00 AM exists


def test_infer_year_normal_window() -> None:
    ref = dt.date(2026, 9, 8)
    assert infer_year(9, 6, ref) == dt.date(2026, 9, 6)
    assert infer_year(8, 31, ref) == dt.date(2026, 8, 31)


def test_infer_year_crosses_new_year() -> None:
    ref = dt.date(2027, 1, 3)
    assert infer_year(12, 28, ref) == dt.date(2026, 12, 28)
    assert infer_year(1, 2, ref) == dt.date(2027, 1, 2)


def test_infer_year_tolerates_a_late_batch() -> None:
    """A 3-day portal lag must not push the oldest column out of the window."""
    ref = dt.date(2026, 9, 12)
    assert infer_year(9, 3, ref) == dt.date(2026, 9, 3)


def test_infer_year_weekday_checksum() -> None:
    ref = dt.date(2026, 9, 8)
    assert infer_year(9, 6, ref, weekday_name="Sun") == dt.date(2026, 9, 6)
    with pytest.raises(DataError, match="weekday mismatch"):
        infer_year(9, 6, ref, weekday_name="Mon")


def test_infer_year_rejects_out_of_window() -> None:
    with pytest.raises(DataError, match="no year places"):
        infer_year(3, 15, dt.date(2026, 9, 8))


def test_infer_year_handles_feb_29_candidates() -> None:
    """A non-leap candidate year must be skipped, not crash."""
    assert infer_year(2, 29, dt.date(2028, 3, 2)) == dt.date(2028, 2, 29)


def test_parse_mmdd_uses_weekday() -> None:
    assert parse_mmdd("09/06", dt.date(2026, 9, 8), "Sun") == dt.date(2026, 9, 6)
    with pytest.raises(DataError):
        parse_mmdd("09/06", dt.date(2026, 9, 8), "Tue")


def test_parse_local_datetime_is_eastern_not_utc() -> None:
    ts = parse_local_datetime("09/06/2026 01:36 AM")
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute) == (2026, 9, 6, 1, 36)
    assert ts.utcoffset() == dt.timedelta(hours=-4)  # EDT, not UTC
    assert ts.astimezone(dt.UTC).hour == 5


def test_parse_local_datetime_rejects_garbage() -> None:
    with pytest.raises(DataError):
        parse_local_datetime("not a date")


def test_eastern_today_uses_eastern_not_utc() -> None:
    """Late-evening Eastern is already tomorrow in UTC; the reference must not slip."""
    late = dt.datetime(2026, 9, 9, 2, 30, tzinfo=dt.UTC)  # 10:30 PM Sep 8 Eastern
    assert eastern_today(late) == dt.date(2026, 9, 8)


# ------------------------------------------------------------ usage day (M3)


def _local(y: int, m: int, d: int, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=EASTERN)


def test_usage_day_small_hours_read_belongs_to_previous_day() -> None:
    from myusage_archive.timeutil import usage_day

    assert usage_day(_local(2026, 8, 14, 1, 32)) == dt.date(2026, 8, 13)   # Failed placeholder
    assert usage_day(_local(2026, 8, 15, 1, 42)) == dt.date(2026, 8, 14)   # 48 h catch-up read
    assert usage_day(_local(2025, 11, 10, 3, 35)) == dt.date(2025, 11, 9)  # winter schedule
    assert usage_day(_local(2025, 9, 11, 0, 5)) == dt.date(2025, 9, 10)


def test_usage_day_late_read_belongs_to_its_own_day() -> None:
    from myusage_archive.timeutil import usage_day

    assert usage_day(_local(2026, 7, 12, 23, 0)) == dt.date(2026, 7, 12)
    assert usage_day(_local(2025, 6, 13, 22, 11)) == dt.date(2025, 6, 13)
    assert usage_day(_local(2026, 1, 1, 12, 0)) == dt.date(2026, 1, 1)     # noon is "late"
    assert usage_day(_local(2026, 1, 1, 11, 59)) == dt.date(2025, 12, 31)


def test_usage_day_is_decided_on_eastern_wall_clock() -> None:
    from myusage_archive.timeutil import usage_day

    # 05:30 UTC on Nov 2 is 01:30 EDT on Nov 1 (fall-back day): previous day, Oct 31.
    assert usage_day(dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.UTC)) == dt.date(2026, 10, 31)


def test_local_midnight_utc_is_top_of_hour_across_dst() -> None:
    from myusage_archive.timeutil import local_midnight_utc

    for day in (dt.date(2026, 3, 8), dt.date(2026, 11, 1), dt.date(2026, 7, 4)):
        assert local_midnight_utc(day) % 3600 == 0
    fall_back = local_midnight_utc(dt.date(2026, 11, 2)) - local_midnight_utc(dt.date(2026, 11, 1))
    assert fall_back == 25 * 3600
