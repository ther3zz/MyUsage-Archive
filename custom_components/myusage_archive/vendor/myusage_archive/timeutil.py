"""Eastern-time handling for portal timestamps.

Every timestamp the portal renders is US Eastern local wall-clock time with no
zone marker. Two hazards drive this module:

1. **Year inference.** The interval grids label day columns `MM/DD` with no
   year, so December data fetched in January must not land in the wrong year.
   The weekday row is used as a checksum.
2. **DST.** Eastern has a 23-hour and a 25-hour day each year. Naive
   arithmetic silently produces wrong UTC instants — subtracting two aware
   datetimes that share a zone yields the *wall clock* difference, not elapsed
   time. Everything here converts through UTC explicitly.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from .exceptions import DataError

EASTERN = ZoneInfo("America/New_York")

# "12:00 AM" (15-minute grid) or "12:00 AM - 01:00 AM" (hourly grid).
_TIME_LABEL_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*([AP]M)\s*(?:-\s*(\d{1,2}):(\d{2})\s*([AP]M)\s*)?$",
    re.IGNORECASE,
)
_MMDD_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*$")
_DATETIME_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*([AP]M)", re.IGNORECASE
)
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")

_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


def parse_time_label(label: str) -> int:
    """Minutes since local midnight for an interval row label.

    Accepts both grid dialects. Returns the interval START (verified by probe
    P10 on 2026-09-08: hourly bucket 12:00-01:00 AM equals the sum of the four
    15-minute rows labelled 12:00, 12:15, 12:30 and 12:45).

    :raises DataError: if the label is not a time.
    """
    match = _TIME_LABEL_RE.match(label)
    if not match:
        raise DataError(f"not a time label: {label!r}")
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if not (1 <= hour <= 12) or minute >= 60:
        raise DataError(f"impossible time label: {label!r}")
    hour = hour % 12
    if meridiem == "PM":
        hour += 12
    return hour * 60 + minute


def is_time_label(label: str) -> bool:
    """True if the row label is a time (i.e. a data row, not a summary row)."""
    try:
        parse_time_label(label)
    except DataError:
        return False
    return True


def day_length_minutes(day: dt.date) -> int:
    """Length of a local calendar day in minutes (1380 / 1440 / 1500).

    Computed through UTC so DST transitions are reflected.
    """
    start = dt.datetime.combine(day, dt.time(0), EASTERN)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), EASTERN)
    delta = end.astimezone(dt.UTC) - start.astimezone(dt.UTC)
    return int(delta.total_seconds() // 60)


def expected_interval_count(day: dt.date, minutes_per_interval: int) -> int:
    """How many intervals a local day should contain (96 / 92 / 100 for 15min)."""
    return day_length_minutes(day) // minutes_per_interval


def localize(day: dt.date, minutes_from_midnight: int, *, second_pass: bool = False) -> dt.datetime:
    """Build an aware Eastern datetime from a local day + WALL-CLOCK offset.

    The portal's row labels are wall-clock times, not elapsed time. On a
    fall-back day that distinction matters for every row after the
    transition: treating "2:00 AM" as 120 elapsed minutes would place it at
    1:00 AM EST, an hour early. `second_pass` selects the repeated hour
    (fold=1) on a fall-back day.
    """
    hour, minute = divmod(minutes_from_midnight, 60)
    if hour >= 24 or minute >= 60:
        raise DataError(f"{minutes_from_midnight} is not a wall-clock time of day")
    naive = dt.datetime.combine(day, dt.time(hour, minute))
    return naive.replace(tzinfo=EASTERN, fold=1 if second_pass else 0)


def day_slots(day: dt.date, minutes_per_interval: int) -> list[tuple[int, int]]:
    """Every (wall-clock minute, occurrence) slot that exists on a local day.

    Yields 96 pairs on a normal day, 92 on spring-forward, 100 on fall-back.
    This is the canonical enumeration for gap math.
    """
    slots: list[tuple[int, int]] = []
    for minutes in range(0, 1440, minutes_per_interval):
        for occurrence in (0, 1):
            if slot_exists(day, minutes, occurrence):
                slots.append((minutes, occurrence))
    return slots


def slot_exists(day: dt.date, minutes_from_midnight: int, occurrence: int = 0) -> bool:
    """Whether a wall-clock slot exists on a given local day.

    Three cases matter:
    * a normal slot exists once (occurrence 0);
    * on a spring-forward day the skipped hour does not exist at all;
    * on a fall-back day the repeated hour exists twice (occurrence 1 valid).
    """
    hour, minute = divmod(minutes_from_midnight, 60)
    if hour >= 24 or minute >= 60:
        return False
    naive = dt.datetime.combine(day, dt.time(hour, minute))
    aware = naive.replace(tzinfo=EASTERN, fold=1 if occurrence else 0)
    # A nonexistent local time does not survive a round trip through UTC.
    round_trip = aware.astimezone(dt.UTC).astimezone(EASTERN)
    if (round_trip.hour, round_trip.minute) != (hour, minute):
        return False
    if occurrence:
        # A second occurrence only exists where the offset actually differs.
        return aware.utcoffset() != aware.replace(fold=0).utcoffset()
    return True


def has_dst_transition(days: list[dt.date]) -> bool:
    """True if any day in the window is not 24 hours long."""
    return any(day_length_minutes(day) != 1440 for day in days)


def infer_year(
    month: int,
    day: int,
    reference: dt.date,
    *,
    weekday_name: str | None = None,
    back_days: int = 14,
    forward_days: int = 2,
) -> dt.date:
    """Resolve a year-less MM/DD against a reference (Eastern) date.

    Chooses the candidate year placing the date inside
    ``[reference - back_days, reference + forward_days]``. The window is wide
    enough to tolerate a late portal batch and modest clock skew, and narrow
    enough that only one year can match. When ``weekday_name`` is supplied it
    must agree with the resolved date — the portal renders a weekday row, and
    a mismatch means our inference (or the layout) is wrong.

    :raises DataError: if no candidate year fits, or the weekday disagrees.
    """
    earliest = reference - dt.timedelta(days=back_days)
    latest = reference + dt.timedelta(days=forward_days)
    candidates: list[dt.date] = []
    for year in {earliest.year, reference.year, latest.year}:
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            continue  # e.g. 02/29 in a non-leap candidate year
        if earliest <= candidate <= latest:
            candidates.append(candidate)

    if not candidates:
        raise DataError(
            f"no year places {month:02d}/{day:02d} within "
            f"{earliest.isoformat()}..{latest.isoformat()} (reference {reference.isoformat()})"
        )
    if len(set(candidates)) > 1:
        raise DataError(
            f"ambiguous year for {month:02d}/{day:02d}: {sorted(set(candidates))}"
        )
    resolved = candidates[0]

    if weekday_name:
        key = weekday_name.strip()[:3].casefold()
        expected = _WEEKDAYS.get(key)
        if expected is None:
            raise DataError(f"unrecognized weekday label: {weekday_name!r}")
        if resolved.weekday() != expected:
            raise DataError(
                f"weekday mismatch for {resolved.isoformat()}: portal says {weekday_name!r} "
                f"but that date is a {resolved.strftime('%a')}"
            )
    return resolved


def parse_mmdd(text: str, reference: dt.date, weekday_name: str | None = None) -> dt.date:
    """Parse an `MM/DD` column header into a full date."""
    match = _MMDD_RE.match(text)
    if not match:
        raise DataError(f"not an MM/DD date header: {text!r}")
    return infer_year(
        int(match.group(1)), int(match.group(2)), reference, weekday_name=weekday_name
    )


def parse_local_datetime(text: str) -> dt.datetime:
    """Parse `MM/DD/YYYY H:MM AM` (as rendered in the daily table) as Eastern."""
    match = _DATETIME_RE.search(text)
    if not match:
        raise DataError(f"not a portal timestamp: {text!r}")
    month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hour, minute, meridiem = int(match.group(4)), int(match.group(5)), match.group(6).upper()
    hour = hour % 12
    if meridiem == "PM":
        hour += 12
    try:
        naive = dt.datetime(year, month, day, hour, minute)
    except ValueError as err:
        raise DataError(f"impossible timestamp {text!r}: {err}") from err
    return naive.replace(tzinfo=EASTERN)


def parse_local_date(text: str) -> dt.date:
    """Parse a bare `MM/DD/YYYY`."""
    match = _DATE_RE.search(text)
    if not match:
        raise DataError(f"not a portal date: {text!r}")
    try:
        return dt.date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
    except ValueError as err:
        raise DataError(f"impossible date {text!r}: {err}") from err


def eastern_today(now: dt.datetime | None = None) -> dt.date:
    """Today's date in Eastern — the correct reference for year inference."""
    moment = now or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(EASTERN).date()
