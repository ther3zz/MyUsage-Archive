"""Cumulative-series planning for statistics export — pure library logic.

Given what the archive holds and what the recorder currently holds for a
statistic, decide exactly which rows to (re)import so that the cumulative
``sum`` series is monotonic, deterministic, and never rewinds. This is the
four-case anchor decision table from the implementation plan (§5), kept free
of HA so it can be tested exhaustively without a Home Assistant instance.

Vocabulary:

* **point** — one row of the series: an *hour* (four 15-minute intervals
  rolled up) or a *day* (a midnight bucket built from the daily Usage
  History table, for days the interval window never covered — plan §5,
  "daily-bucket backfill").
* **final point** — one whose value can no longer change from the portal:
  a complete hour, an hour older than the portal's rolling window (a
  permanent partial), or a day bucket (eligible only once the day is
  permanently outside the window).
* **final prefix** — the contiguous run of final points from the series'
  first point. Export never skips a still-recoverable hole, because filling
  it later would shift every subsequent sum.
* **anchor** — the newest row the recorder already holds for the statistic.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .archive import DailyBucket, HourlyBucket
from .timeutil import EASTERN

Field = Literal["delivered", "received"]
Action = Literal["none", "full", "append", "reimport", "halt"]
PointKind = Literal["hour", "day"]

HOUR = 3600
SUM_EPSILON = 1e-6  # below the 0.001 kWh data quantum, above float noise


@dataclass(frozen=True, slots=True)
class Anchor:
    """Newest recorder row for a statistic (epoch seconds, float sum)."""

    start_utc: int
    sum: float


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One exportable row of the series, before folding into sums."""

    kind: PointKind
    start_utc: int
    end_utc: int
    kwh_delivered: Decimal | None
    kwh_received: Decimal | None
    final: bool

    def value(self, field: Field) -> Decimal | None:
        return self.kwh_delivered if field == "delivered" else self.kwh_received

    @property
    def day(self) -> dt.date | None:
        """The local day a day point represents (None for hour points)."""
        if self.kind != "day":
            return None
        return dt.datetime.fromtimestamp(self.start_utc, dt.UTC).astimezone(EASTERN).date()


@dataclass(frozen=True, slots=True)
class SeriesRow:
    start_utc: int
    state: Decimal
    sum: Decimal


@dataclass(frozen=True, slots=True)
class Plan:
    action: Action
    reason: str
    rows: tuple[SeriesRow, ...] = ()

    @property
    def from_utc(self) -> int | None:
        return self.rows[0].start_utc if self.rows else None

    @property
    def last(self) -> SeriesRow | None:
        return self.rows[-1] if self.rows else None


# --------------------------------------------------------------- points


def hour_point(bucket: HourlyBucket, oldest_recoverable_utc: int) -> SeriesPoint:
    return SeriesPoint(
        kind="hour",
        start_utc=bucket.start_utc,
        end_utc=bucket.start_utc + HOUR,
        kwh_delivered=bucket.kwh_delivered,
        kwh_received=bucket.kwh_received,
        final=bucket.complete or bucket.start_utc < oldest_recoverable_utc,
    )


def day_point(bucket: DailyBucket) -> SeriesPoint:
    return SeriesPoint(
        kind="day",
        start_utc=bucket.start_utc,
        end_utc=bucket.end_utc,
        kwh_delivered=bucket.kwh_delivered,
        kwh_received=bucket.kwh_received,
        final=True,
    )


def build_points(
    hourly: Iterable[HourlyBucket],
    daily: Iterable[DailyBucket],
    *,
    oldest_recoverable_utc: int,
) -> list[SeriesPoint]:
    """Merge hourly buckets with the day buckets that are eligible to fill in.

    A day is bucket-eligible only when it has **zero archived intervals**
    and is **permanently outside the portal window** (its whole day ends
    before the oldest still-fetchable midnight). The leading edge therefore
    always waits for intervals; a day never flips from bucket to hours in
    normal operation. A day with even one archived interval is represented
    by its (possibly partial) hours only — the daily table never patches a
    partially captured day, because that would double count.
    """
    hours = sorted(hourly, key=lambda b: b.start_utc)
    hour_starts = [b.start_utc for b in hours]
    points = [hour_point(b, oldest_recoverable_utc) for b in hours]

    for bucket in sorted(daily, key=lambda b: b.start_utc):
        if bucket.end_utc > oldest_recoverable_utc:
            continue  # still inside the window; intervals may yet arrive
        lo = bisect.bisect_left(hour_starts, bucket.start_utc)
        hi = bisect.bisect_left(hour_starts, bucket.end_utc)
        if hi > lo:
            continue  # the day has intervals; hours represent it
        points.append(day_point(bucket))
    points.sort(key=lambda p: p.start_utc)
    return points


def final_prefix(points: list[SeriesPoint], oldest_recoverable_utc: int) -> list[SeriesPoint]:
    """The contiguous run of final points from the start of the series.

    Stops at the first point that could still change: an incomplete hour
    inside the portal window, or a hole (a span with no point at all) inside
    it. Holes and partial hours older than the window are permanent and pass
    through — a partial hour exports its partial sum, a hole exports nothing.
    """
    prefix: list[SeriesPoint] = []
    previous_end: int | None = None
    for point in sorted(points, key=lambda p: p.start_utc):
        if previous_end is not None and point.start_utc > previous_end:
            hole_start = previous_end
            if hole_start >= oldest_recoverable_utc:
                break  # a missing span we may still fetch
        if not point.final:
            break
        prefix.append(point)
        previous_end = point.end_utc
    return prefix


def fold(points: list[SeriesPoint], field: Field, base: Decimal) -> tuple[SeriesRow, ...]:
    """Exact cumulative sums: identical input always yields identical rows.

    A final hour with no value contributes a zero-state row: the hour
    existed and the portal never published it, and a statistics row saying
    "nothing attributed here" keeps the series contiguous (the archive
    itself still holds NULL). A day bucket with no value emits **no** row —
    the portal left the day blank, and there is no hour to attribute to.
    """
    rows: list[SeriesRow] = []
    running = base
    for point in points:
        value = point.value(field)
        if value is None:
            if point.kind == "day":
                continue
            value = Decimal(0)
        running += value
        rows.append(SeriesRow(start_utc=point.start_utc, state=value, sum=running))
    return tuple(rows)


def merge_stray_rows(
    rows: tuple[SeriesRow, ...], existing_starts: Iterable[int], base: Decimal
) -> tuple[SeriesRow, ...]:
    """Union the plan's rows with starts the recorder already holds in range.

    A re-import rewrites a contiguous span, but the recorder may hold a row
    at a start the new representation no longer produces — the midnight
    bucket of a day that has since gained intervals (a "flip", plan §5), or
    an hour that is now a hole. Left alone, that row keeps its old ``sum``
    and draws a rewind or a phantom bar. Each such stray is rewritten as a
    zero-state row carrying the running sum at that moment: a statistics
    attribution of "nothing here", never a fabricated reading.
    """
    if not rows:
        return rows
    first, last = rows[0].start_utc, rows[-1].start_utc
    strays = sorted(
        s for s in set(existing_starts)
        if first <= s <= last and all(s != r.start_utc for r in rows)
    )
    if not strays:
        return rows
    merged: list[SeriesRow] = []
    running = base
    pending = iter(strays)
    stray = next(pending, None)
    for row in rows:
        while stray is not None and stray < row.start_utc:
            merged.append(SeriesRow(start_utc=stray, state=Decimal(0), sum=running))
            stray = next(pending, None)
        merged.append(row)
        running = row.sum
    return tuple(merged)


# ------------------------------------------------------------------ plan


def plan_series(
    points: list[SeriesPoint],
    field: Field,
    *,
    anchor: Anchor | None,
    pre_series_sum: Decimal,
    changed_from_utc: int | None,
    oldest_recoverable_utc: int,
    epsilon: float = SUM_EPSILON,
) -> Plan:
    """Decide what to import for one statistic.

    ``pre_series_sum`` is the recorder's sum on the last row *before* the
    series' first point (0 if none): our series continues from any history
    the recorder already has, so sums never rewind at the seam.
    ``changed_from_utc`` is the earliest series start affected by an archive
    write since the last export (revisions, late rows, a backfill); ``None``
    means nothing changed.
    """
    prefix = final_prefix(points, oldest_recoverable_utc)
    if not prefix:
        return Plan("none", "no final points to export")

    first, last = prefix[0].start_utc, prefix[-1].start_utc

    if anchor is None:
        # Case 1: no statistics yet (first run, recorder wiped, or the user
        # deleted the series). Build everything from the archive.
        return Plan("full", "no existing statistics", fold(prefix, field, pre_series_sum))

    if anchor.start_utc > last:
        # Case 4: the recorder is ahead of the archive (archive rolled back).
        # Rewriting would rewind sums; refuse and let a human decide.
        return Plan(
            "halt",
            f"recorder anchor {anchor.start_utc} is newer than the newest final "
            f"archived point {last}",
        )

    if anchor.start_utc < first:
        # Recorder history predates the archive: append everything, continuing
        # from the recorder's own last sum so the series stays monotonic.
        rows = fold(prefix, field, Decimal(repr(anchor.sum)))
        return Plan("append", "recorder history predates the archive; continuing its sum", rows)

    all_rows = fold(prefix, field, pre_series_sum)
    at_anchor = next((r for r in all_rows if r.start_utc == anchor.start_utc), None)

    if at_anchor is None:
        # The anchor is a permanent hole in the archive; nothing to verify
        # against. Rebuild deterministically from the archive.
        return Plan("full", f"anchor {anchor.start_utc} is absent from the archive", all_rows)

    if changed_from_utc is not None and changed_from_utc <= anchor.start_utc:
        # Revision-driven repair: a known archive change at or before the
        # anchor explains any sum mismatch, so re-import contiguously from the
        # earliest changed point through the last final point with fresh
        # running sums. The next cycle's anchor check verifies the result;
        # anything still inconsistent then becomes a full rebuild.
        from_hour = changed_from_utc - (changed_from_utc % HOUR)
        rows = tuple(r for r in all_rows if r.start_utc >= from_hour)
        return Plan("reimport", f"archive changed at or before the anchor (from {from_hour})", rows)

    if abs(float(at_anchor.sum) - anchor.sum) > epsilon:
        # Case 3: sums disagree with no known change to explain it (manual
        # edits, a different archive, an old bug). Full deterministic rebuild.
        return Plan(
            "full",
            f"anchor sum mismatch at {anchor.start_utc}: archive {at_anchor.sum} vs "
            f"recorder {anchor.sum}",
            all_rows,
        )

    # Case 2 / normal path: append strictly after the anchor.
    rows = tuple(r for r in all_rows if r.start_utc > anchor.start_utc)
    if not rows:
        return Plan("none", "up to date")
    return Plan("append", f"{len(rows)} new row(s) after the anchor", rows)
