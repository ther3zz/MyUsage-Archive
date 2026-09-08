"""MyUsage Archive: durable capture of MyUsage (OUC) interval data + Energy statistics.

Unofficial; not affiliated with Exceleron Software or OUC.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import MyUsageConfigEntry, MyUsageCoordinator, archive_path

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: MyUsageConfigEntry) -> bool:
    coordinator = MyUsageCoordinator(hass, entry)
    entry.async_on_unload(coordinator.cancel_timer)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MyUsageConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        entry.runtime_data.cancel_timer()
        await entry.runtime_data.async_release_backup_lock()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: MyUsageConfigEntry) -> None:
    """Delete the archive database when the entry is removed (documented)."""
    base = Path(archive_path(hass, entry.entry_id))

    def _delete() -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(base) + suffix)
            if path.exists():
                path.unlink()
        _LOGGER.info("removed archive %s", base)

    await hass.async_add_executor_job(_delete)
