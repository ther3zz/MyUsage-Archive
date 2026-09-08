"""Diagnostics download: redacted entry data plus an archive summary."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_EMAIL, CONF_PASSWORD, STAT_DELIVERED, STAT_RECEIVED
from .coordinator import MyUsageConfigEntry
from .exporter import statistic_id
from .vendor.myusage_archive.redact import scrub_text

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, "title", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MyUsageConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    archive = coordinator.archive
    run = hass.async_add_executor_job

    stats = await run(archive.stats)
    fetches = await run(archive.fetch_history, 15)
    meters = await run(archive.meters)
    exporter_state: dict[str, Any] = {}
    gaps: dict[str, Any] | None = None
    issues: list[str] = []
    if coordinator.meter:
        meter = coordinator.meter
        for kind in (STAT_DELIVERED, STAT_RECEIVED):
            sid = statistic_id(meter, kind)
            state = await run(archive.exporter_state, sid)
            exporter_state[sid] = None if state is None else {
                "anchor_start_utc": state.anchor_start_utc,
                "anchor_sum": str(state.anchor_sum),
                "exported_at_utc": state.exported_at_utc,
            }
        report = await run(lambda: archive.gaps(meter))
        gaps = {
            "first_day": report.first_day.isoformat() if report.first_day else None,
            "last_publishable_day": report.last_publishable_day.isoformat(),
            "permanent_missing": report.permanent_missing,
            "recoverable_missing": report.recoverable_missing,
            "incomplete_days": [
                {"day": g.day.isoformat(), "missing": g.missing, "recoverable": g.recoverable}
                for g in report.incomplete_days[:30]
            ],
        }
        issues = [
            f"{i.kind}: {i.detail}" for i in await run(lambda: archive.consistency_report(meter))
        ][:30]

    data = coordinator.data
    return {
        "entry": async_redact_data(
            {"data": dict(entry.data), "options": dict(entry.options), "title": entry.title},
            TO_REDACT,
        ),
        "meter": [asdict(m) for m in meters],
        "archive": {**stats, "path": "<config>/…/" + stats["path"].rsplit("/", 1)[-1]},
        "recent_fetches": [
            {**f, "error": scrub_text(f["error"]) if f.get("error") else None} for f in fetches
        ],
        "gaps": gaps,
        "consistency": issues,
        "exporter_state": exporter_state,
        "last_export": None
        if data is None or data.export is None
        else {
            "series": [asdict(s) for s in data.export.series],
            "halted": [asdict(s) for s in data.export.halted],
            "hour_points": data.export.hour_points,
            "day_points": data.export.day_points,
        },
        "coordinator": None
        if data is None
        else {
            "last_fetch": data.last_fetch.isoformat() if data.last_fetch else None,
            "newest_interval": data.newest_interval.isoformat() if data.newest_interval else None,
            "interval_rows": data.interval_rows,
            "last_cycle_new_rows": data.last_cycle_new_rows,
            "fetched_this_refresh": data.fetched_this_refresh,
        },
    }
