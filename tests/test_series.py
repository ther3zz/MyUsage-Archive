"""The statistics anchor decision table, exhaustively, without Home Assistant.

Every case here corresponds to a way the prior integration corrupted the
Energy dashboard (rewinding sums, re-baselining, negative bars) or to a
scenario from the adversarial design review (recorder wiped, restored from an
older backup, user deleted the series, archive changed after export, archive
rolled back, two entries fighting).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from myusage_archive.archive import HourlyBucket
from myusage_archive.series import (
    HOUR,
    Anchor,
    final_prefix,
    fold,
    plan_series,
)

T0 = int(dt.datetime(2026, 9, 1, 4, 0, tzinfo=dt.UTC).timestamp())  # midnight Eastern
FAR_FUTURE = T0 + 400 * HOUR   # everything is "older than the window"
FAR_PAST = T0 - 400 * HOUR     # everything is "still recoverable"


def bucket(i: int, delivered: str | None = "1.000", received: str | None = "0.250",
           present: int = 4, missing: int = 0) -> HourlyBucket:
    return HourlyBucket(
        start_utc=T0 + i * HOUR,
        kwh_delivered=None if delivered is None else Decimal(delivered),
        kwh_received=None if received is None else Decimal(received),
        intervals_present=present,
        intervals_expected=4,
        delivered_missing=missing,
        received_missing=0,
    )


def hours(n: int) -> list[HourlyBucket]:
    return [bucket(i) for i in range(n)]


# ------------------------------------------------------------ final prefix


def test_final_prefix_stops_at_incomplete_recoverable_hour() -> None:
    buckets = hours(5)
    buckets[3] = bucket(3, present=3, missing=1)
    prefix = final_prefix(buckets, oldest_recoverable_utc=FAR_PAST)
    assert [b.start_utc for b in prefix] == [T0 + i * HOUR for i in range(3)]


def test_final_prefix_exports_partial_hour_once_window_has_passed() -> None:
    buckets = hours(5)
    buckets[3] = bucket(3, present=3, missing=1)
    prefix = final_prefix(buckets, oldest_recoverable_utc=FAR_FUTURE)
    assert len(prefix) == 5


def test_final_prefix_blocks_at_a_recoverable_hole() -> None:
    buckets = [bucket(0), bucket(1), bucket(3), bucket(4)]  # hour 2 missing
    prefix = final_prefix(buckets, oldest_recoverable_utc=FAR_PAST)
    assert len(prefix) == 2


def test_final_prefix_skips_a_permanent_hole() -> None:
    buckets = [bucket(0), bucket(1), bucket(3), bucket(4)]
    prefix = final_prefix(buckets, oldest_recoverable_utc=FAR_FUTURE)
    assert [b.start_utc for b in prefix] == [T0, T0 + HOUR, T0 + 3 * HOUR, T0 + 4 * HOUR]


def test_final_prefix_is_order_independent() -> None:
    buckets = list(reversed(hours(4)))
    assert [b.start_utc for b in final_prefix(buckets, FAR_PAST)] == sorted(
        b.start_utc for b in buckets
    )


# -------------------------------------------------------------------- fold


def test_fold_is_exact_and_deterministic() -> None:
    buckets = [bucket(i, delivered="0.1") for i in range(10)]
    rows = fold(buckets, "delivered", Decimal(0))
    assert rows[-1].sum == Decimal("1.0")  # 0.1 * 10 exactly; floats would drift
    assert fold(buckets, "delivered", Decimal(0)) == rows


def test_fold_continues_from_base() -> None:
    rows = fold(hours(2), "received", Decimal("100"))
    assert [r.sum for r in rows] == [Decimal("100.25"), Decimal("100.5")]
    assert [r.state for r in rows] == [Decimal("0.25"), Decimal("0.25")]


def test_fold_treats_missing_value_as_zero_attribution() -> None:
    buckets = [bucket(0), bucket(1, delivered=None), bucket(2)]
    rows = fold(buckets, "delivered", Decimal(0))
    assert [r.state for r in rows] == [Decimal(1), Decimal(0), Decimal(1)]
    assert rows[-1].sum == Decimal(2)


# ---------------------------------------------------------- decision table


def _plan(buckets, **kw):
    defaults = dict(anchor=None, pre_series_sum=Decimal(0), changed_from_utc=None,
                    oldest_recoverable_utc=FAR_PAST)
    defaults.update(kw)
    return plan_series(buckets, "delivered", **defaults)


def test_case1_no_statistics_full_import_from_zero() -> None:
    plan = _plan(hours(3))
    assert plan.action == "full"
    assert [float(r.sum) for r in plan.rows] == [1.0, 2.0, 3.0]
    assert plan.from_utc == T0


def test_case2_matching_anchor_appends_after_it() -> None:
    plan = _plan(hours(5), anchor=Anchor(T0 + 2 * HOUR, 3.0))
    assert plan.action == "append"
    assert [r.start_utc for r in plan.rows] == [T0 + 3 * HOUR, T0 + 4 * HOUR]
    assert [float(r.sum) for r in plan.rows] == [4.0, 5.0]  # continues the chain


def test_up_to_date_is_a_no_op() -> None:
    plan = _plan(hours(3), anchor=Anchor(T0 + 2 * HOUR, 3.0))
    assert plan.action == "none"
    assert plan.rows == ()


def test_case2_anchor_behind_watermark_is_just_an_append() -> None:
    """Recorder restored from an older backup: recorder position wins."""
    plan = _plan(hours(6), anchor=Anchor(T0 + 1 * HOUR, 2.0))
    assert plan.action == "append"
    assert plan.from_utc == T0 + 2 * HOUR
    assert len(plan.rows) == 4


def test_case3_sum_mismatch_triggers_full_rebuild() -> None:
    plan = _plan(hours(4), anchor=Anchor(T0 + 2 * HOUR, 99.0))
    assert plan.action == "full"
    assert "mismatch" in plan.reason
    assert [float(r.sum) for r in plan.rows] == [1.0, 2.0, 3.0, 4.0]


def test_case3_rebuild_continues_from_recorder_history_before_archive() -> None:
    """Rows before the archive's first hour keep their sums; ours build on them."""
    plan = _plan(hours(3), anchor=Anchor(T0 + 1 * HOUR, 99.0), pre_series_sum=Decimal("50"))
    assert plan.action == "full"
    assert [float(r.sum) for r in plan.rows] == [51.0, 52.0, 53.0]


def test_case4_recorder_ahead_of_archive_halts() -> None:
    plan = _plan(hours(3), anchor=Anchor(T0 + 10 * HOUR, 11.0))
    assert plan.action == "halt"
    assert plan.rows == ()


def test_recorder_history_predating_archive_continues_its_sum() -> None:
    plan = _plan(hours(3), anchor=Anchor(T0 - 5 * HOUR, 40.0))
    assert plan.action == "append"
    assert [float(r.sum) for r in plan.rows] == [41.0, 42.0, 43.0]


def test_revision_before_anchor_reimports_contiguously_from_changed_hour() -> None:
    buckets = hours(6)
    buckets[2] = bucket(2, delivered="5.000")  # corrected value
    plan = _plan(
        buckets,
        anchor=Anchor(T0 + 4 * HOUR, 5.0),  # recorder still has the old chain
        changed_from_utc=T0 + 2 * HOUR + 900,  # a 15-min interval inside hour 2
    )
    # The anchor sum (5.0) no longer matches the corrected fold, but the change
    # is known, so the repair is a contiguous re-import from the changed hour
    # (not a full rebuild): rows before hour 2 keep their sums.
    assert plan.action == "reimport"
    assert plan.from_utc == T0 + 2 * HOUR
    assert [float(r.sum) for r in plan.rows] == [7.0, 8.0, 9.0, 10.0]


def test_late_fill_after_anchor_hour_with_matching_anchor_reimports_from_change() -> None:
    """A change strictly inside an hour before the anchor whose sum still
    matches (e.g. received changed but we are planning delivered) re-imports
    from that hour, never from scratch."""
    buckets = hours(6)
    plan = _plan(
        buckets,
        anchor=Anchor(T0 + 4 * HOUR, 5.0),
        changed_from_utc=T0 + 1 * HOUR + 1800,
    )
    assert plan.action == "reimport"
    assert plan.from_utc == T0 + 1 * HOUR
    assert [r.start_utc for r in plan.rows] == [T0 + i * HOUR for i in range(1, 6)]


def test_change_after_anchor_is_a_plain_append() -> None:
    plan = _plan(hours(6), anchor=Anchor(T0 + 2 * HOUR, 3.0), changed_from_utc=T0 + 4 * HOUR)
    assert plan.action == "append"
    assert plan.from_utc == T0 + 3 * HOUR


def test_anchor_in_a_permanent_hole_rebuilds() -> None:
    buckets = [bucket(0), bucket(1), bucket(3), bucket(4)]
    plan = _plan(buckets, anchor=Anchor(T0 + 2 * HOUR, 2.0), oldest_recoverable_utc=FAR_FUTURE)
    assert plan.action == "full"
    assert "absent" in plan.reason


def test_nothing_final_nothing_planned() -> None:
    buckets = [bucket(0, present=1, missing=3)]
    plan = _plan(buckets)
    assert plan.action == "none"


def test_received_series_uses_received_values() -> None:
    plan = plan_series(hours(2), "received", anchor=None, pre_series_sum=Decimal(0),
                       changed_from_utc=None, oldest_recoverable_utc=FAR_PAST)
    assert [float(r.state) for r in plan.rows] == [0.25, 0.25]


def test_sums_are_monotonic_in_every_plan() -> None:
    buckets = hours(12)
    for anchor in (None, Anchor(T0 + 3 * HOUR, 4.0), Anchor(T0 + 3 * HOUR, 1.0),
                   Anchor(T0 - HOUR, 7.5)):
        plan = _plan(buckets, anchor=anchor)
        sums = [r.sum for r in plan.rows]
        assert sums == sorted(sums)
