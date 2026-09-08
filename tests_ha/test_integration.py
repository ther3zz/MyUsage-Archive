"""End-to-end against a real (in-memory) recorder: setup, export, repair, backup, flows."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.myusage_archive.const import (
    CONF_EMAIL,
    CONF_FETCH_TIME,
    CONF_JITTER_MINUTES,
    CONF_KEEP_RAW_PAGES,
    CONF_PASSWORD,
    DOMAIN,
    ISSUE_LAYOUT_ERROR,
)
from custom_components.myusage_archive.exporter import statistic_id
from myusage_archive.exceptions import AuthenticationError, LayoutError
from myusage_archive.models import IntervalReading, Resolution

from .conftest import METER

DELIVERED = statistic_id(METER, "energy_delivered")
RECEIVED = statistic_id(METER, "energy_received")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def _last(hass: HomeAssistant, sid: str) -> dict | None:
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, sid, True, {"sum", "state"}
    )
    return (rows.get(sid) or [None])[0]


async def _all_rows(hass: HomeAssistant, sid: str) -> list[dict]:
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt.datetime.fromtimestamp(0, dt.UTC),
        None,
        {sid},
        "hour",
        None,
        {"sum", "state"},
    )
    return rows.get(sid) or []


# ------------------------------------------------------------- statistics


async def test_setup_exports_both_series_from_a_seeded_archive(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, seeded
) -> None:
    """Startup with a fresh archive touches no network and exports statistics."""
    with patch("custom_components.myusage_archive.coordinator.Pipeline") as pipeline:
        await _setup(hass, entry)
        pipeline.assert_not_called()  # 6-hour startup guard

    assert entry.state is ConfigEntryState.LOADED
    delivered = await _all_rows(hass, DELIVERED)
    received = await _all_rows(hass, RECEIVED)
    assert len(delivered) == 168 and len(received) == 168
    # Exact totals from the live capture, as a float of the decimal fold.
    assert delivered[-1]["sum"] == pytest.approx(432.294, abs=1e-9)
    assert received[-1]["sum"] == pytest.approx(207.642, abs=1e-9)
    sums = [r["sum"] for r in delivered]
    assert sums == sorted(sums)  # monotonic
    assert all(r["state"] >= 0 for r in delivered)

    data = entry.runtime_data.data
    assert data.meter == METER and data.has_received
    assert data.fetched_this_refresh is False
    assert data.export is not None and data.export.imported_rows == 336


async def test_second_refresh_is_idempotent(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, seeded
) -> None:
    with patch("custom_components.myusage_archive.coordinator.Pipeline"):
        await _setup(hass, entry)
        before = await _all_rows(hass, DELIVERED)
        coordinator = entry.runtime_data
        # Force a non-network refresh path: the startup guard applies to the
        # first refresh only, so stub the cycle to "nothing new".
        with patch.object(coordinator, "_fetch_cycle", AsyncMock(return_value=0)):
            await coordinator.async_refresh()
            await async_wait_recording_done(hass)
    after = await _all_rows(hass, DELIVERED)
    assert after == before
    assert coordinator.data.export is not None
    assert [s.action for s in coordinator.data.export.series] == ["none", "none"]


async def test_revision_triggers_contiguous_reimport(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, seeded, monkeypatch
) -> None:
    """A corrected value after export re-imports from that hour, sums shift, no rewind."""
    with patch("custom_components.myusage_archive.coordinator.Pipeline"):
        await _setup(hass, entry)
        coordinator = entry.runtime_data
        before = await _all_rows(hass, DELIVERED)
        # The export watermark has 1-second resolution; make the correction
        # unambiguously "later" than the export, as it would be in real life.
        import time

        monkeypatch.setattr("myusage_archive.archive._now_utc", lambda: int(time.time()) + 60)

        # Correct the first interval of the 3rd day (+1 kWh) as a later fetch would.
        target = seeded.intervals(METER)[2 * 96]
        corrected = IntervalReading(
            meter=METER, start=target.start, resolution=Resolution.FIFTEEN_MIN,
            kwh_delivered=target.kwh_delivered + Decimal("1"),
            kwh_received=target.kwh_received, temperature_f=target.temperature_f,
        )

        def _revise() -> None:
            fid = seeded.record_fetch("grid15", ok=True)
            seeded.store_intervals([corrected], meter=METER, fetch_id=fid)

        await hass.async_add_executor_job(_revise)
        with patch.object(coordinator, "_fetch_cycle", AsyncMock(return_value=0)):
            await coordinator.async_refresh()
            await async_wait_recording_done(hass)

    after = await _all_rows(hass, DELIVERED)
    changed_hour = target.start_utc - target.start_utc % 3600
    for old, new in zip(before, after, strict=True):
        if new["start"] < changed_hour:
            assert new["sum"] == old["sum"]
        else:
            assert new["sum"] == pytest.approx(old["sum"] + 1.0, abs=1e-9)
    sums = [r["sum"] for r in after]
    assert sums == sorted(sums)
    actions = {s.statistic_id: s.action for s in coordinator.data.export.series}
    assert actions[DELIVERED] == "reimport"


async def test_deleted_series_is_rebuilt(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, seeded
) -> None:
    """Anchor case 1: the user cleared the statistic; next refresh rebuilds it."""
    with patch("custom_components.myusage_archive.coordinator.Pipeline"):
        await _setup(hass, entry)
        coordinator = entry.runtime_data
        get_instance(hass).async_clear_statistics([DELIVERED])
        await async_wait_recording_done(hass)
        assert await _last(hass, DELIVERED) is None
        with patch.object(coordinator, "_fetch_cycle", AsyncMock(return_value=0)):
            await coordinator.async_refresh()
            await async_wait_recording_done(hass)
    rows = await _all_rows(hass, DELIVERED)
    assert len(rows) == 168
    assert rows[-1]["sum"] == pytest.approx(432.294, abs=1e-9)


# ------------------------------------------------------------- fetch path


async def test_auth_failure_starts_reauth(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Empty archive => first refresh fetches; bad credentials => reauth flow."""
    with patch("custom_components.myusage_archive.coordinator.Pipeline") as pipeline:
        pipeline.return_value.run_cycle = AsyncMock(side_effect=AuthenticationError("nope"))
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"].get("source") == "reauth" for f in flows)


async def test_layout_error_creates_repair_issue(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    with patch("custom_components.myusage_archive.coordinator.Pipeline") as pipeline:
        pipeline.return_value.run_cycle = AsyncMock(side_effect=LayoutError("stride 5"))
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_LAYOUT_ERROR}_{entry.entry_id}") is not None


# ------------------------------------------------------------------ backup


async def test_backup_hooks_lock_and_release(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, seeded
) -> None:
    from custom_components.myusage_archive import backup

    with patch("custom_components.myusage_archive.coordinator.Pipeline"):
        await _setup(hass, entry)
    coordinator = entry.runtime_data
    await backup.async_pre_backup(hass)
    assert coordinator._backup_lock is not None and coordinator._backup_lock.held  # noqa: SLF001
    await backup.async_post_backup(hass)
    assert coordinator._backup_lock is None  # noqa: SLF001
    # The archive is usable again afterwards.
    assert await hass.async_add_executor_job(seeded.integrity_check) == "ok"


# ------------------------------------------------------------------ sensors


async def test_diagnostic_sensors(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, seeded
) -> None:
    with patch("custom_components.myusage_archive.coordinator.Pipeline"):
        await _setup(hass, entry)
    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    assert len(states) == 4
    gaps = next(s for s in states.values() if s.entity_id.endswith("permanently_missing_intervals"))
    assert gaps.state == "0"
    newest = next(s for s in states.values() if s.entity_id.endswith("newest_archived_interval"))
    assert newest.state.startswith("2026-09-07T03:45:00")  # 23:45 Eastern on Sep 6, in UTC


# ------------------------------------------------------------ config flows


async def test_user_flow_success_and_duplicate(recorder_mock, hass: HomeAssistant) -> None:
    with patch("custom_components.myusage_archive.config_flow._validate_login",
               AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "Me@Example.com", CONF_PASSWORD: "pw"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Me@Example.com"
        assert result["result"].unique_id == "me@example.com"

        dup = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        dup = await hass.config_entries.flow.async_configure(
            dup["flow_id"], {CONF_EMAIL: "me@example.com", CONF_PASSWORD: "pw"}
        )
        assert dup["type"] is FlowResultType.ABORT
        assert dup["reason"] == "already_configured"


@pytest.mark.parametrize("error", ["invalid_auth", "cannot_connect", "unsupported_account"])
async def test_user_flow_errors(recorder_mock, hass: HomeAssistant, error: str) -> None:
    with patch("custom_components.myusage_archive.config_flow._validate_login",
               AsyncMock(return_value=error)):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "a@b.c", CONF_PASSWORD: "pw"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_reauth_flow_updates_password(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, seeded
) -> None:
    with patch("custom_components.myusage_archive.coordinator.Pipeline"):
        await _setup(hass, entry)
    with patch("custom_components.myusage_archive.config_flow._validate_login",
               AsyncMock(return_value=None)):
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-pw"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-pw"
    assert entry.data[CONF_EMAIL] == "user@example.com"


async def test_options_flow_validates_time(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, seeded
) -> None:
    with patch("custom_components.myusage_archive.coordinator.Pipeline"):
        await _setup(hass, entry)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        bad = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_FETCH_TIME: "25:99", CONF_JITTER_MINUTES: 5, CONF_KEEP_RAW_PAGES: 2},
        )
        assert bad["errors"] == {CONF_FETCH_TIME: "invalid_time"}
        good = await hass.config_entries.options.async_configure(
            bad["flow_id"],
            {CONF_FETCH_TIME: "13:05", CONF_JITTER_MINUTES: 5, CONF_KEEP_RAW_PAGES: 2},
        )
        await hass.async_block_till_done()
    assert good["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_FETCH_TIME] == "13:05"


# -------------------------------------------------------------- lifecycle


async def test_remove_entry_deletes_archive(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, seeded
) -> None:
    import os

    with patch("custom_components.myusage_archive.coordinator.Pipeline"):
        await _setup(hass, entry)
    path = str(seeded.path)
    assert os.path.exists(path)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not os.path.exists(path)
