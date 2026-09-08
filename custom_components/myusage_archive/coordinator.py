"""Daily fetch scheduling, archive maintenance and statistics export.

Why this is not a plain ``update_interval``: the portal publishes once a day
at ~10:28 AM Eastern with a two-day lag, so the useful moment to fetch is a
fixed Eastern wall-clock time. The next fire is computed in America/New_York
with zoneinfo and armed with ``async_track_point_in_time``, which is
DST-correct by construction and honest to what the option says.

Startup never hammers the portal: if the archive already has a recent
successful fetch, the first refresh only recomputes state from the archive
and exports statistics; a network fetch is scheduled for two minutes later
only when the archive is stale. A missed day is recoverable for a week, so
the retry ladder is short and lives in memory.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ARCHIVE_DIR,
    CONF_EMAIL,
    CONF_FETCH_TIME,
    CONF_JITTER_MINUTES,
    CONF_KEEP_RAW_PAGES,
    CONF_PASSWORD,
    DEFAULT_FETCH_TIME,
    DEFAULT_JITTER_MINUTES,
    DEFAULT_KEEP_RAW_PAGES,
    DOMAIN,
    ISSUE_EXPORT_HALTED,
    ISSUE_LAYOUT_ERROR,
    ISSUE_STALE,
    ISSUE_UNSUPPORTED_ACCOUNT,
    RETRY_DELAYS_MINUTES,
    STALE_AFTER_HOURS,
    STARTUP_FETCH_MIN_AGE_HOURS,
)
from .exporter import ExportReport, StatisticsExporter
from .vendor.myusage_archive.archive import Archive, BackupLock
from .vendor.myusage_archive.client import MyUsageClient
from .vendor.myusage_archive.exceptions import (
    AuthenticationError,
    DataError,
    LayoutError,
    MfaRequiredError,
    MyUsageError,
    UnsupportedAccountError,
)
from .vendor.myusage_archive.service import Pipeline
from .vendor.myusage_archive.timeutil import EASTERN, eastern_today

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MyUsageData:
    """Coordinator state, all derived from the archive."""

    meter: str | None
    has_received: bool
    last_fetch: dt.datetime | None
    newest_interval: dt.datetime | None
    permanent_missing: int
    recoverable_missing: int
    interval_rows: int
    last_cycle_new_rows: int | None
    export: ExportReport | None
    fetched_this_refresh: bool


def archive_path(hass: HomeAssistant, entry_id: str) -> str:
    return hass.config.path(ARCHIVE_DIR, f"{entry_id}.db")


class MyUsageCoordinator(DataUpdateCoordinator[MyUsageData]):
    """One config entry == one MyUsage account == one archive database."""

    config_entry: MyUsageConfigEntry

    def __init__(self, hass: HomeAssistant, entry: MyUsageConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=None,  # scheduled explicitly, see _schedule_next
        )
        self.archive = Archive(
            archive_path(hass, entry.entry_id),
            keep_ok_raw_pages=int(entry.options.get(CONF_KEEP_RAW_PAGES, DEFAULT_KEEP_RAW_PAGES)),
        )
        self.exporter = StatisticsExporter(hass, self.archive, self._run_blocking)
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._retry_index = 0
        self._backup_lock: BackupLock | None = None
        self._meter: str | None = None
        self._has_received = False
        # A statistics-only integration may have no entity listeners; keep
        # the coordinator alive regardless (opower's dummy-listener idiom).
        self.async_add_listener(lambda: None)

    # ------------------------------------------------------------ plumbing

    async def _run_blocking(self, func: Callable[[], Any]) -> Any:
        return await self.hass.async_add_executor_job(func)

    @property
    def meter(self) -> str | None:
        return self._meter

    @callback
    def cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    def _option(self, key: str, default: Any) -> Any:
        return self.config_entry.options.get(key, default)

    def _next_daily_fire(self, now: dt.datetime) -> dt.datetime:
        fetch_time = str(self._option(CONF_FETCH_TIME, DEFAULT_FETCH_TIME))
        hour, minute = (int(part) for part in fetch_time.split(":"))
        jitter = int(self._option(CONF_JITTER_MINUTES, DEFAULT_JITTER_MINUTES))
        local_now = now.astimezone(EASTERN)
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        candidate += dt.timedelta(minutes=random.uniform(0, max(jitter, 0)))  # noqa: S311
        if candidate <= local_now:
            candidate += dt.timedelta(days=1)
        return candidate

    def _schedule(self, when: dt.datetime, reason: str) -> None:
        self.cancel_timer()
        _LOGGER.debug("next fetch at %s (%s)", when.isoformat(), reason)

        @callback
        def _fire(_now: dt.datetime) -> None:
            self._unsub_timer = None
            self.hass.async_create_task(self.async_refresh())

        self._unsub_timer = async_track_point_in_time(self.hass, _fire, when)

    def _schedule_next(self, *, new_rows: int | None, failed: bool) -> None:
        """Short in-memory retry ladder, then the next daily slot."""
        now = dt_util.utcnow()
        if (failed or new_rows == 0) and self._retry_index < len(RETRY_DELAYS_MINUTES):
            delay = RETRY_DELAYS_MINUTES[self._retry_index]
            self._retry_index += 1
            self._schedule(now + dt.timedelta(minutes=delay), f"retry {self._retry_index}")
            return
        self._retry_index = 0
        self._schedule(self._next_daily_fire(now), "daily slot")

    # ------------------------------------------------------------- issues

    def _issue(self, issue_id: str, key: str, placeholders: dict[str, str]) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{issue_id}_{self.config_entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=key,
            translation_placeholders=placeholders,
        )

    def _clear_issue(self, issue_id: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, f"{issue_id}_{self.config_entry.entry_id}")

    # -------------------------------------------------------------- refresh

    async def _async_setup(self) -> None:
        """One-time: make sure the archive opens (creates the schema) off-loop."""
        meters = await self._run_blocking(self.archive.meters)
        if len(meters) == 1:
            self._meter = meters[0].meter_number
            self._has_received = meters[0].has_received

    async def _async_update_data(self) -> MyUsageData:
        email: str = self.config_entry.data[CONF_EMAIL]
        last_ok = await self._run_blocking(self.archive.last_successful_fetch_utc)
        now_utc = int(dt_util.utcnow().timestamp())
        first_run = self.data is None
        fresh = last_ok is not None and now_utc - last_ok < STARTUP_FETCH_MIN_AGE_HOURS * 3600
        # Startup with a recent archive: do not touch the portal.
        do_network = not (first_run and fresh)

        new_rows: int | None = None
        try:
            if do_network:
                new_rows = await self._fetch_cycle(email)
        except ConfigEntryAuthFailed:
            self.cancel_timer()  # HA starts re-authentication; nothing to schedule
            raise
        except UpdateFailed:
            if first_run:
                self.cancel_timer()  # HA retries the whole setup (ConfigEntryNotReady)
                raise
            self._schedule_next(new_rows=None, failed=True)
            raise

        if first_run and not do_network and (last_ok is None or now_utc - last_ok > 26 * 3600):
            self._schedule(dt_util.utcnow() + dt.timedelta(minutes=2), "stale at startup")
        else:
            self._schedule_next(new_rows=new_rows, failed=False)

        export = None
        if self._meter is not None:
            export = await self.exporter.async_export(
                self._meter, has_received=self._has_received, today_eastern=eastern_today()
            )
            for halted in export.halted:
                self._issue(
                    f"{ISSUE_EXPORT_HALTED}_{halted.statistic_id.replace(':', '_')}",
                    ISSUE_EXPORT_HALTED,
                    {"statistic_id": halted.statistic_id, "reason": halted.reason},
                )

        return await self._build_data(email, new_rows, export, do_network)

    async def _fetch_cycle(self, email: str) -> int:
        """Run one portal cycle; map library errors onto HA semantics."""
        session = async_create_clientsession(self.hass, cookie_jar=aiohttp.CookieJar())
        client = MyUsageClient(email, self.config_entry.data[CONF_PASSWORD], session)
        pipeline = Pipeline(client, self.archive, run_blocking=self._run_blocking)
        try:
            result = await pipeline.run_cycle(meter=self._meter)
        except (AuthenticationError, MfaRequiredError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except UnsupportedAccountError as err:
            self._issue(ISSUE_UNSUPPORTED_ACCOUNT, ISSUE_UNSUPPORTED_ACCOUNT,
                        {"email": email, "error": str(err)})
            raise UpdateFailed(f"unsupported account: {err}") from err
        except (LayoutError, DataError) as err:
            self._issue(ISSUE_LAYOUT_ERROR, ISSUE_LAYOUT_ERROR, {"email": email, "error": str(err)})
            raise UpdateFailed(f"page layout not understood: {err}") from err
        except MyUsageError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            await session.close()

        self._clear_issue(ISSUE_LAYOUT_ERROR)
        self._clear_issue(ISSUE_UNSUPPORTED_ACCOUNT)
        self._meter = result.meter
        meters = await self._run_blocking(self.archive.meters)
        self._has_received = any(m.meter_number == result.meter and m.has_received for m in meters)
        store = result.intervals.store
        return store.inserted if store else 0

    async def _build_data(
        self, email: str, new_rows: int | None, export: ExportReport | None, fetched: bool
    ) -> MyUsageData:
        last_ok = await self._run_blocking(self.archive.last_successful_fetch_utc)
        stats = await self._run_blocking(self.archive.stats)
        permanent = recoverable = 0
        newest: dt.datetime | None = None
        if self._meter is not None:
            meter = self._meter
            report = await self._run_blocking(lambda: self.archive.gaps(meter))
            permanent, recoverable = report.permanent_missing, report.recoverable_missing
            rng = await self._run_blocking(lambda: self.archive.interval_range(meter))
            if rng:
                newest = dt.datetime.fromtimestamp(rng[1], dt.UTC)

        last_fetch = dt.datetime.fromtimestamp(last_ok, dt.UTC) if last_ok else None
        if last_fetch and (dt_util.utcnow() - last_fetch) > dt.timedelta(hours=STALE_AFTER_HOURS):
            self._issue(ISSUE_STALE, ISSUE_STALE, {"email": email, "hours": str(STALE_AFTER_HOURS)})
        else:
            self._clear_issue(ISSUE_STALE)

        return MyUsageData(
            meter=self._meter,
            has_received=self._has_received,
            last_fetch=last_fetch,
            newest_interval=newest,
            permanent_missing=permanent,
            recoverable_missing=recoverable,
            interval_rows=int(stats["rows"]["interval_readings"]),
            last_cycle_new_rows=new_rows,
            export=export,
            fetched_this_refresh=fetched,
        )

    # -------------------------------------------------------------- backup

    async def async_lock_for_backup(self) -> None:
        if self._backup_lock is None:
            self._backup_lock = await self._run_blocking(self.archive.lock_for_backup)

    async def async_release_backup_lock(self) -> None:
        lock, self._backup_lock = self._backup_lock, None
        if lock is not None:
            await self._run_blocking(lock.release)


MyUsageConfigEntry = ConfigEntry[MyUsageCoordinator]
