"""Cumulative-series planning for statistics export — pure library logic.

Given the archive's hourly buckets and what the recorder currently holds for
a statistic, decide exactly which rows to (re)import so that the cumulative
``sum`` series is monotonic, deterministic, and never rewinds. This is the
four-case anchor decision table from the implementation plan (§5), kept free
of HA so it can be tested exhaustively without a Home Assistant instance.

Vocabulary:

* **final hour** — an hour whose value can no longer change from the portal:
  all four 15-minute intervals are present with a delivered value, *or* the
  hour is older than the portal's rolling window (a permanent partial).
* **final prefix** — the contiguous run of final hours from the archive's
  first hour. Export never skips a still-recoverable hole, because filling it
  later would shift every subsequent sum.
* **anchor** — the newest row the recorder already holds for the statistic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .archive import HourlyBucket

Field = Literal["delivered", "received"]
Action = Literal["none", "full", "append", "reimport", "halt"]

HOUR = 3600
SUM_EPSILON = 1e-6  # below the 0.001 kWh data quantum, above float noise


@dataclass(frozen=True, slots=True)
class Anchor:
    """Newest recorder row for a statistic (epoch seconds, float sum)."""

    start_utc: int
    sum: float


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


def _value(bucket: HourlyBucket, field: Field) -> Decimal:
    value = bucket.kwh_delivered if field == "delivered" else bucket.kwh_received
    # A final-but-empty hour contributes nothing. This is a statistics
    # attribution for an hour the portal never published, not a fabricated
    # reading — the archive itself still holds NULL.
    return value if value is not None else Decimal(0)


def final_prefix(buckets: list[HourlyBucket], oldest_recoverable_utc: int) -> list[HourlyBucket]:
    """The contiguous run of final hours from the start of the archive.

    Stops at the first hour that could still change: an incomplete hour inside
    the portal window, or a hole (an hour with no rows at all) inside it.
    Holes and partial hours older than the window are permanent and pass
    through — a partial hour exports its partial sum, a hole exports nothing.
    """
    prefix: list[HourlyBucket] = []
    previous_end: int | None = None
    for bucket in sorted(buckets, key=lambda b: b.start_utc):
        if previous_end is not None and bucket.start_utc > previous_end:
            hole_start = previous_end
            if hole_start >= oldest_recoverable_utc:
                break  # a missing hour we may still fetch
        final = bucket.complete or bucket.start_utc < oldest_recoverable_utc
        if not final:
            break
        prefix.append(bucket)
        previous_end = bucket.start_utc + HOUR
    return prefix


def fold(buckets: list[HourlyBucket], field: Field, base: Decimal) -> tuple[SeriesRow, ...]:
    """Exact cumulative sums: identical input always yields identical rows."""
    rows: list[SeriesRow] = []
    running = base
    for bucket in buckets:
        state = _value(bucket, field)
        running += state
        rows.append(SeriesRow(start_utc=bucket.start_utc, state=state, sum=running))
    return tuple(rows)


def plan_series(
    buckets: list[HourlyBucket],
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
    archive's first hour (0 if none): our series continues from any history
    the recorder already has, so sums never rewind at the seam.
    ``changed_from_utc`` is the earliest archive write since the last export
    (revisions or late-arriving rows); ``None`` means nothing changed.
    """
    prefix = final_prefix(buckets, oldest_recoverable_utc)
    if not prefix:
        return Plan("none", "no final hours to export")

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
            f"archived hour {last}",
        )

    if anchor.start_utc < first:
        # Recorder history predates the archive: append everything, continuing
        # from the recorder's own last sum so the series stays monotonic.
        rows = fold(prefix, field, Decimal(repr(anchor.sum)))
        return Plan("append", "recorder history predates the archive; continuing its sum", rows)

    all_rows = fold(prefix, field, pre_series_sum)
    at_anchor = next((r for r in all_rows if r.start_utc == anchor.start_utc), None)

    if at_anchor is None:
        # The anchor hour is a permanent hole in the archive; nothing to
        # verify against. Rebuild deterministically from the archive.
        return Plan("full", f"anchor hour {anchor.start_utc} is absent from the archive", all_rows)

    if changed_from_utc is not None and changed_from_utc <= anchor.start_utc:
        # Revision-driven repair: a known archive change at or before the
        # anchor explains any sum mismatch, so re-import contiguously from the
        # earliest changed hour through the last final hour with fresh running
        # sums. The next cycle's anchor check verifies the result; anything
        # still inconsistent then becomes a full rebuild.
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
    return Plan("append", f"{len(rows)} new hour(s) after the anchor", rows)
