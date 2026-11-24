"""Area occupancy from Vive occupancy sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import OCCUPIED, UNOCCUPIED
from .entity import ViveAreaEntity
from .hub import ViveArea, ViveHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up occupancy sensors for areas that report occupancy."""
    hub = entry.runtime_data
    async_add_entities(
        ViveOccupancy(hub, entry, area)
        for area in hub.areas.values()
        if area.occupancy in (OCCUPIED, UNOCCUPIED)
    )


class ViveOccupancy(ViveAreaEntity, BinarySensorEntity):
    """Occupancy of one area."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_translation_key = "occupancy"

    def __init__(self, hub: ViveHub, entry: ConfigEntry, area: ViveArea) -> None:
        """Initialize the sensor."""
        super().__init__(hub, entry, area)
        self._attr_unique_id = f"{self._hub_id}_area_{area.id}_occupancy"

    @property
    def is_on(self) -> bool | None:
        """Return True when occupied, None when the hub does not know."""
        area = self._area
        if area is None or area.occupancy not in (OCCUPIED, UNOCCUPIED):
            return None
        return area.occupancy == OCCUPIED
