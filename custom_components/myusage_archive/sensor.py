"""Diagnostic sensors. The Energy dashboard reads the statistics, not these.

No live power or energy entities on purpose: the data is ~2 days old and
would be misleading as "current" state. These sensors exist to make freshness
and gaps visible.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_EMAIL, DOMAIN
from .coordinator import MyUsageConfigEntry, MyUsageCoordinator, MyUsageData

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MyUsageSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MyUsageData], dt.datetime | int | None]


SENSORS: tuple[MyUsageSensorDescription, ...] = (
    MyUsageSensorDescription(
        key="last_fetch",
        translation_key="last_fetch",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.last_fetch,
    ),
    MyUsageSensorDescription(
        key="newest_interval",
        translation_key="newest_interval",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.newest_interval,
    ),
    MyUsageSensorDescription(
        key="permanent_gaps",
        translation_key="permanent_gaps",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="intervals",
        value_fn=lambda d: d.permanent_missing,
    ),
    MyUsageSensorDescription(
        key="recoverable_gaps",
        translation_key="recoverable_gaps",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="intervals",
        value_fn=lambda d: d.recoverable_missing,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MyUsageConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(MyUsageSensor(coordinator, entry, desc) for desc in SENSORS)


class MyUsageSensor(CoordinatorEntity[MyUsageCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: MyUsageSensorDescription

    def __init__(
        self,
        coordinator: MyUsageCoordinator,
        entry: MyUsageConfigEntry,
        description: MyUsageSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        meter = coordinator.meter
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"MyUsage {meter}" if meter else f"MyUsage {entry.data[CONF_EMAIL]}",
            manufacturer="Exceleron MyUsage (unofficial archiver)",
            model="Postpaid electric",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> dt.datetime | int | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
