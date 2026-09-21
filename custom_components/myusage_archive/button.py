"""A "Fetch now" button, next to the diagnostic sensors.

The schedule stays the rule: the portal publishes once a day and one fetch a
day is all the data supports. This button is for the moments when waiting for
the slot is the wrong answer - after a re-authentication, after fixing a
network problem, or while setting the integration up. It runs exactly the
cycle the timer would run and leaves the timer alone.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MyUsageConfigEntry, MyUsageCoordinator, device_info

PARALLEL_UPDATES = 0

FETCH_NOW = ButtonEntityDescription(
    key="fetch_now",
    translation_key="fetch_now",
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MyUsageConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([MyUsageFetchNowButton(entry.runtime_data, entry)])


class MyUsageFetchNowButton(ButtonEntity):
    """Not a CoordinatorEntity on purpose: a button that disappears after a
    failed fetch is unpressable exactly when it is wanted."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MyUsageCoordinator, entry: MyUsageConfigEntry) -> None:
        self.coordinator = coordinator
        self.entity_description = FETCH_NOW
        self._attr_unique_id = f"{entry.entry_id}_{FETCH_NOW.key}"
        self._attr_device_info = device_info(coordinator, entry)

    async def async_press(self) -> None:
        await self.coordinator.async_fetch_now()
