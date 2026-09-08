"""Backup platform: keep the archive consistent inside Home Assistant backups.

HA tars the database and its write-ahead log one after the other, so a commit
landing in between produces a torn pair. Mirroring the recorder's own backup
platform, each entry's archive is checkpointed and write-locked for the
duration of the backup. Failures never leave a lock behind.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import MyUsageCoordinator

_LOGGER = logging.getLogger(__name__)


def _coordinators(hass: HomeAssistant) -> list[MyUsageCoordinator]:
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        if isinstance(getattr(entry, "runtime_data", None), MyUsageCoordinator)
    ]


async def async_pre_backup(hass: HomeAssistant) -> None:
    """Checkpoint and write-lock every archive before the backup starts."""
    locked: list[MyUsageCoordinator] = []
    try:
        for coordinator in _coordinators(hass):
            await coordinator.async_lock_for_backup()
            locked.append(coordinator)
    except Exception:
        for coordinator in locked:
            await coordinator.async_release_backup_lock()
        raise
    _LOGGER.debug("locked %d archive(s) for backup", len(locked))


async def async_post_backup(hass: HomeAssistant) -> None:
    """Release the locks after the backup finishes (or fails)."""
    for coordinator in _coordinators(hass):
        await coordinator.async_release_backup_lock()
