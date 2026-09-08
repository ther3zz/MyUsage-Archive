"""Push archived hours into Home Assistant long-term statistics.

Two external statistics per meter (grid consumption and grid return), each a
strictly non-negative, monotonic cumulative series derived from the archive by
:mod:`myusage_archive.series`. This module owns only the Home Assistant plumbing: reading the
recorder's anchor, building metadata and rows, queueing the import, and
recording the watermark afterwards (import first, watermark second, so a crash
between them costs one harmless idempotent re-import).

Verified against Home Assistant 2026.9 (plan §1.2): ``StatisticMetaData``
requires every field including ``unit_class``; rows must carry exactly
``start``/``state``/``sum`` because an update replaces the whole row; the
import is fire-and-forget, so nothing here reads back what it just wrote.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util.unit_conversion import EnergyConverter

from myusage_archive.archive import Archive, HourlyBucket
from myusage_archive.series import Anchor, Field, Plan, plan_series
from myusage_archive.timeutil import EASTERN

from .const import DOMAIN, STAT_DELIVERED, STAT_RECEIVED

_LOGGER = logging.getLogger(__name__)

RunBlocking = Callable[[Callable[[], Any]], Awaitable[Any]]

# Recorder statistic ids are lowercase slugs: no '__', no edge underscores.
_SLUG_BAD = re.compile(r"[^a-z0-9_]+")
_SLUG_DUP = re.compile(r"_{2,}")

# Consecutive full rebuilds of one series before we stop and raise an issue —
# the signature of two entries fighting over one statistic id.
THRASH_LIMIT = 3

PORTAL_LAG_DAYS = 2
PORTAL_WINDOW_DAYS = 7


def slugify_meter(meter: str) -> str:
    slug = _SLUG_DUP.sub("_", _SLUG_BAD.sub("_", meter.casefold())).strip("_")
    return slug or "meter"


def statistic_id(meter: str, kind: str) -> str:
    return f"{DOMAIN}:{slugify_meter(meter)}_{kind}"


def build_metadata(meter: str, kind: str) -> StatisticMetaData:
    label = "delivered" if kind == STAT_DELIVERED else "received"
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"MyUsage {meter} {label}",
        source=DOMAIN,
        statistic_id=statistic_id(meter, kind),
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def oldest_recoverable_utc(today_eastern: dt.date) -> int:
    """Epoch of local midnight on the oldest day still inside the portal window."""
    oldest_day = today_eastern - dt.timedelta(days=PORTAL_LAG_DAYS + PORTAL_WINDOW_DAYS - 1)
    return int(dt.datetime.combine(oldest_day, dt.time(0), EASTERN).timestamp())


@dataclass(frozen=True, slots=True)
class SeriesReport:
    statistic_id: str
    action: str
    reason: str
    rows: int
    from_utc: int | None = None


@dataclass(slots=True)
class ExportReport:
    series: list[SeriesReport] = field(default_factory=list)
    halted: list[SeriesReport] = field(default_factory=list)

    @property
    def imported_rows(self) -> int:
        return sum(s.rows for s in self.series)


class StatisticsExporter:
    """Exports one meter's archive into two external statistics."""

    def __init__(self, hass: HomeAssistant, archive: Archive, run_blocking: RunBlocking) -> None:
        self._hass = hass
        self._archive = archive
        self._run = run_blocking
        self._consecutive_full: dict[str, int] = {}

    # -- recorder reads (blocking; run on the recorder's own executor)

    async def _anchor(self, stat_id: str) -> Anchor | None:
        result = await get_instance(self._hass).async_add_executor_job(
            get_last_statistics, self._hass, 1, stat_id, True, {"sum"}
        )
        rows = result.get(stat_id) or []
        if not rows:
            return None
        total = rows[0].get("sum")
        if total is None:
            return None
        return Anchor(start_utc=int(rows[0]["start"]), sum=float(total))

    async def _sum_before(self, stat_id: str, before_utc: int) -> Decimal:
        """Recorder sum on the last row strictly before ``before_utc`` (0 if none)."""
        start = dt.datetime.fromtimestamp(0, dt.UTC)
        end = dt.datetime.fromtimestamp(before_utc, dt.UTC)
        result = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            start,
            end,
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
        for row in reversed(result.get(stat_id) or []):
            total = row.get("sum")
            if total is not None:
                return Decimal(repr(float(total)))
        return Decimal(0)

    # -- export

    async def async_export(
        self, meter: str, *, has_received: bool, today_eastern: dt.date
    ) -> ExportReport:
        report = ExportReport()
        buckets: list[HourlyBucket] = await self._run(lambda: self._archive.hourly_series(meter))
        if not buckets:
            return report
        oldest = oldest_recoverable_utc(today_eastern)
        kinds: list[tuple[str, Field]] = [(STAT_DELIVERED, "delivered")]
        if has_received:
            kinds.append((STAT_RECEIVED, "received"))

        for kind, field_name in kinds:
            stat_id = statistic_id(meter, kind)
            state = await self._run(functools.partial(self._archive.exporter_state, stat_id))
            changed_from = None
            if state is not None:
                changed_from = await self._run(
                    functools.partial(
                        self._archive.earliest_interval_change_since, meter, state.exported_at_utc
                    )
                )
            anchor = await self._anchor(stat_id)
            first_utc = min(b.start_utc for b in buckets)
            pre_sum = Decimal(0)
            if anchor is not None and anchor.start_utc >= first_utc:
                pre_sum = await self._sum_before(stat_id, first_utc)

            plan = plan_series(
                buckets,
                field_name,
                anchor=anchor,
                pre_series_sum=pre_sum,
                changed_from_utc=changed_from,
                oldest_recoverable_utc=oldest,
            )
            report_entry = SeriesReport(
                stat_id, plan.action, plan.reason, len(plan.rows), plan.from_utc
            )

            if plan.action == "halt":
                report.halted.append(report_entry)
                _LOGGER.error("export halted for %s: %s", stat_id, plan.reason)
                continue

            if plan.action == "full":
                count = self._consecutive_full.get(stat_id, 0) + 1
                self._consecutive_full[stat_id] = count
                if count >= THRASH_LIMIT:
                    halted = SeriesReport(
                        stat_id, "halt",
                        f"{count} consecutive full rebuilds — is another entry writing "
                        f"this statistic? ({plan.reason})",
                        0,
                    )
                    report.halted.append(halted)
                    _LOGGER.error("export halted for %s: %s", stat_id, halted.reason)
                    continue
            else:
                self._consecutive_full[stat_id] = 0

            if plan.action == "none" or not plan.rows:
                report.series.append(report_entry)
                continue

            self._import(meter, kind, plan)
            last = plan.last
            assert last is not None
            await self._run(
                functools.partial(
                    self._archive.set_exporter_state,
                    stat_id,
                    anchor_start_utc=last.start_utc,
                    anchor_sum=last.sum,
                )
            )
            report.series.append(report_entry)
            _LOGGER.info(
                "%s: %s %d row(s) from %s (%s)",
                stat_id, plan.action, len(plan.rows),
                dt.datetime.fromtimestamp(plan.from_utc or 0, dt.UTC).isoformat(),
                plan.reason,
            )
        return report

    def _import(self, meter: str, kind: str, plan: Plan) -> None:
        rows = [
            StatisticData(
                start=dt.datetime.fromtimestamp(r.start_utc, dt.UTC),
                state=float(r.state),
                sum=float(r.sum),
            )
            for r in plan.rows
        ]
        async_add_external_statistics(self._hass, build_metadata(meter, kind), rows)
