"""The fetch pipeline: portal → parser → archive, with failures made durable.

This is the one place that knows the whole sequence. Both the CLI and the
Home Assistant coordinator call it; neither re-implements it.

Blocking archive work is handed to ``run_blocking`` (default
``asyncio.to_thread``) so the archive is never touched from the event loop.
Home Assistant supplies ``hass.async_add_executor_job`` instead.

Failure policy: when a page fetches but does not parse, the raw page and the
error are recorded in the archive *before* the exception propagates. That is
what makes a portal layout change diagnosable after the fact instead of
being a mystery in a log line.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from .archive import Archive, GapReport, StoreResult
from .client import MyUsageClient
from .const import GRID_15MIN_ID, GRID_DAILY_ID
from .exceptions import (
    DataError,
    LayoutError,
    MyUsageError,
    TransportError,
    UnsupportedAccountError,
)
from .models import LayoutSignature
from .parser import parse_daily_history, parse_interval_grid
from .redact import scrub_text
from .timeutil import eastern_today, local_midnight_utc

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")
RunBlocking = Callable[[Callable[[], T]], Awaitable[T]]

KIND_INTERVALS = "grid15"
KIND_DAILY = "daily"
KIND_DAILY_RANGE = "daily_range"   # the date-range POST used for backfill


async def _to_thread(func: Callable[[], T]) -> T:
    return await asyncio.to_thread(func)


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """What one fetch+parse+store did."""

    kind: str
    fetch_id: int
    ok: bool
    store: StoreResult | None = None
    signature: LayoutSignature | None = None
    error: str | None = None
    anomalies: tuple[str, ...] = ()
    meters: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CycleResult:
    meter: str
    daily: FetchOutcome | None
    intervals: FetchOutcome
    gaps: GapReport | None = None
    issues: tuple[str, ...] = field(default_factory=tuple)
    backfill: FetchOutcome | None = None


class Pipeline:
    """One authenticated client + one archive."""

    def __init__(
        self,
        client: MyUsageClient,
        archive: Archive,
        *,
        run_blocking: RunBlocking[object] | None = None,
        reference_date: dt.date | None = None,
    ) -> None:
        self.client = client
        self.archive = archive
        self._run = run_blocking or _to_thread
        self._reference_date = reference_date

    async def _blocking(self, func: Callable[[], T]) -> T:
        return await self._run(func)  # type: ignore[return-value]

    # -- individual steps

    async def fetch_intervals(self, *, meter: str) -> FetchOutcome:
        """Fetch the 15-minute grid, parse it, archive it."""
        html: str | None = None
        try:
            html = await self.client.fetch_interval_page()
        except TransportError as err:
            status, message = err.status, str(err)
            fetch_id = await self._blocking(
                lambda: self.archive.record_fetch(
                    KIND_INTERVALS, ok=False, http_status=status, error=message
                )
            )
            _LOGGER.error("interval fetch failed (fetch %s): %s", fetch_id, message)
            raise

        reference = self._reference_date or eastern_today()
        try:
            grid = parse_interval_grid(html, table_id=GRID_15MIN_ID, reference_date=reference)
        except (LayoutError, DataError) as err:
            page, message = html, str(err)
            fetch_id = await self._blocking(
                lambda: self.archive.record_fetch(
                    KIND_INTERVALS, ok=False, http_status=200, error=message, raw_html=page
                )
            )
            _LOGGER.error(
                "interval page did not parse; raw page kept as fetch %s: %s", fetch_id, message
            )
            raise

        page = html
        starts = [r.start_utc for r in grid.readings]
        fetch_id = await self._blocking(
            lambda: self.archive.record_fetch(
                KIND_INTERVALS,
                ok=True,
                http_status=200,
                raw_html=page,
                layout_signature=grid.signature,
                window_start_utc=min(starts) if starts else None,
                window_end_utc=max(starts) if starts else None,
            )
        )
        store = await self._blocking(
            lambda: self.archive.store_intervals(
                grid.readings, meter=meter, fetch_id=fetch_id, has_received=grid.has_received
            )
        )
        for anomaly in grid.signature.anomalies:
            _LOGGER.warning("interval grid anomaly (fetch %s): %s", fetch_id, anomaly)
        _LOGGER.info(
            "intervals: fetch %s stored %s new, %s updated, %s unchanged (%s revisions)",
            fetch_id, store.inserted, store.updated, store.unchanged, store.revisions,
        )
        return FetchOutcome(
            kind=KIND_INTERVALS,
            fetch_id=fetch_id,
            ok=True,
            store=store,
            signature=grid.signature,
            anomalies=grid.signature.anomalies,
            meters=(meter,),
        )

    async def fetch_daily(
        self, *, from_date: dt.date | None = None, to_date: dt.date | None = None
    ) -> FetchOutcome:
        """Fetch the daily Usage History (default 30-day GET, or a date-range POST).

        A range fetch is recorded under its own kind with the window that was
        *requested* (not what came back), so a later cycle can tell whether a
        span has already been asked for even when the portal returned less.
        """
        html: str | None = None
        ranged = from_date is not None or to_date is not None
        kind = KIND_DAILY_RANGE if ranged else KIND_DAILY
        window: tuple[int | None, int | None] = (None, None)
        try:
            if not ranged:
                html = await self.client.fetch_history_page()
            else:
                today = eastern_today()
                start = from_date or (today - dt.timedelta(days=30))
                end = to_date or (today + dt.timedelta(days=1))
                window = (local_midnight_utc(start), local_midnight_utc(end))
                html = await self.client.post_daily_history(
                    start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y"), "Electric"
                )
        except TransportError as err:
            status, message = err.status, str(err)
            fetch_id = await self._blocking(
                lambda: self.archive.record_fetch(
                    kind, ok=False, http_status=status, error=message
                )
            )
            _LOGGER.error("%s fetch failed (fetch %s): %s", kind, fetch_id, message)
            raise

        try:
            daily = parse_daily_history(html)
        except (LayoutError, DataError) as err:
            page, message = html, str(err)
            fetch_id = await self._blocking(
                lambda: self.archive.record_fetch(
                    kind, ok=False, http_status=200, error=message, raw_html=page
                )
            )
            _LOGGER.error(
                "%s page did not parse; raw page kept as fetch %s: %s", kind, fetch_id, message
            )
            raise

        page = html
        froms = [int(r.from_ts.timestamp()) for r in daily.reads]
        if not ranged:
            window = (min(froms) if froms else None, max(froms) if froms else None)
        window_start, window_end = window
        fetch_id = await self._blocking(
            lambda: self.archive.record_fetch(
                kind,
                ok=True,
                http_status=200,
                raw_html=page,
                layout_signature={"table_id": GRID_DAILY_ID, "columns": list(daily.columns),
                                  "rows": len(daily.reads), "excluded": list(daily.excluded_rows)},
                window_start_utc=window_start,
                window_end_utc=window_end,
            )
        )
        store = await self._blocking(
            lambda: self.archive.store_daily(daily.reads, fetch_id=fetch_id)
        )
        _LOGGER.info(
            "%s: fetch %s stored %s new, %s updated, %s unchanged (%s revisions)",
            kind, fetch_id, store.inserted, store.updated, store.unchanged, store.revisions,
        )
        return FetchOutcome(
            kind=kind, fetch_id=fetch_id, ok=True, store=store, meters=daily.meters
        )

    # -- backfill

    def backfill_needed(self, days: int) -> bool:
        """Whether a range POST covering ``today - days`` has not been made yet.

        Blocking (archive read); callers run it via ``run_blocking``.
        """
        if days <= 0:
            return False
        wanted = local_midnight_utc(eastern_today() - dt.timedelta(days=days))
        covered = self.archive.range_fetch_covered_from_utc(KIND_DAILY_RANGE)
        return covered is None or covered > wanted

    async def backfill_daily(self, *, days: int) -> FetchOutcome:
        """One date-range POST for the last ``days`` days of daily history.

        The portal keeps roughly fifteen months (verified 2026-09-08 with a
        25-month request); asking for more simply returns what exists. Every
        row lands through the normal daily upsert, so the statistics exporter
        sees the backfill as one large late arrival and re-sums from the
        earliest day it now represents (plan §5).
        """
        today = eastern_today()
        return await self.fetch_daily(
            from_date=today - dt.timedelta(days=days), to_date=today + dt.timedelta(days=1)
        )

    # -- the daily cycle

    async def run_cycle(
        self, *, meter: str | None = None, backfill_days: int = 0
    ) -> CycleResult:
        """Login, learn the meter from the daily table, archive intervals and daily reads.

        The interval grid carries no meter identifier, so the daily table is
        the source of truth for which meter the intervals belong to. Exactly
        one meter is supported until multi-meter rendering has been observed.

        With ``backfill_days`` > 0, a one-shot date-range POST for that span
        runs after the default daily fetch, once per requested span (it is
        skipped when an earlier successful range fetch already asked for at
        least that much).
        """
        await self.client.login()

        daily = await self.fetch_daily()
        backfill: FetchOutcome | None = None
        if backfill_days > 0 and await self._blocking(
            lambda: self.backfill_needed(backfill_days)
        ):
            backfill = await self.backfill_daily(days=backfill_days)
        meters = tuple(daily.meters)
        if meter is None:
            if len(meters) != 1:
                raise UnsupportedAccountError(
                    f"expected exactly one meter in the daily table, saw {list(meters)}; "
                    "multi-meter accounts are not supported yet (pass meter= explicitly)"
                )
            meter = meters[0]
        elif meters and meter not in meters:
            raise UnsupportedAccountError(
                f"meter {meter!r} not present in the daily table ({list(meters)})"
            )

        intervals = await self.fetch_intervals(meter=meter)
        gaps = await self._blocking(lambda: self.archive.gaps(meter))
        issues = await self._blocking(lambda: self.archive.consistency_report(meter))
        for gap in gaps.incomplete_days:
            level = logging.WARNING if not gap.recoverable else logging.INFO
            _LOGGER.log(
                level, "gap: %s missing %s/%s intervals (%s)",
                gap.day, gap.missing, gap.expected,
                "recoverable" if gap.recoverable else "PERMANENT",
            )
        for issue in issues:
            _LOGGER.log(
                logging.WARNING if issue.severity == "warning" else logging.INFO,
                "consistency: %s: %s", issue.kind, issue.detail,
            )
        return CycleResult(
            meter=meter, daily=daily, intervals=intervals, gaps=gaps,
            issues=tuple(f"{i.kind}: {i.detail}" for i in issues), backfill=backfill,
        )


# ------------------------------------------------------------------ reparse


@dataclass(frozen=True, slots=True)
class ReparseOutcome:
    page_id: int
    kind: str
    fetch_id: int | None
    store: StoreResult | None
    error: str | None = None


def reparse(
    archive: Archive, *, meter: str, reference_dates: dict[int, dt.date] | None = None
) -> list[ReparseOutcome]:
    """Re-run the parser over every retained raw page and re-store the results.

    This is the correction path after a parser fix: a page that previously
    failed (or parsed wrongly) is re-ingested through the normal upsert, so
    any value differences land in ``revisions`` and reach the statistics
    exporter like any other change. Synchronous — the CLI calls it outside
    any event loop.
    """
    outcomes: list[ReparseOutcome] = []
    for page in archive.raw_pages():
        if not page.has_content:
            continue
        html = archive.raw_page_content(page.id)
        if html is None:
            continue
        fetch_id = archive.fetch_for_raw_page(page.id)
        if fetch_id is None:
            fetch_id = archive.record_fetch(page.kind, ok=True, error="reparse: orphan page")
        # Year inference must use the date the page was captured, in Eastern —
        # a January re-parse of a December page must not shift it a year.
        reference = (reference_dates or {}).get(page.id) or eastern_today(
            dt.datetime.fromtimestamp(page.fetched_at_utc, dt.UTC)
        )
        try:
            if page.kind == KIND_INTERVALS:
                grid = parse_interval_grid(html, table_id=GRID_15MIN_ID, reference_date=reference)
                store = archive.store_intervals(
                    grid.readings, meter=meter, fetch_id=fetch_id, has_received=grid.has_received
                )
            elif page.kind in (KIND_DAILY, KIND_DAILY_RANGE):
                daily = parse_daily_history(html)
                store = archive.store_daily(daily.reads, fetch_id=fetch_id)
            else:
                continue
        except MyUsageError as err:
            outcomes.append(
                ReparseOutcome(page.id, page.kind, fetch_id, None, scrub_text(str(err)))
            )
            _LOGGER.warning("reparse: page %s (%s) still fails: %s", page.id, page.kind, err)
            continue
        outcomes.append(ReparseOutcome(page.id, page.kind, fetch_id, store))
        _LOGGER.info(
            "reparse: page %s (%s): %s new, %s updated, %s revisions",
            page.id, page.kind, store.inserted, store.updated, store.revisions,
        )
    return outcomes
