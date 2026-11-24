"""Area power draw reported by the Vive hub."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import ViveAreaEntity
from .hub import ViveArea, ViveHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up power sensors for areas that report power."""
    hub = entry.runtime_data
    async_add_entities(
        VivePower(hub, entry, area)
        for area in hub.areas.values()
        if area.power is not None
    )


class VivePower(ViveAreaEntity, SensorEntity):
    """Instantaneous lighting power of one area."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "power"

    def __init__(self, hub: ViveHub, entry: ConfigEntry, area: ViveArea) -> None:
        """Initialize the sensor."""
        super().__init__(hub, entry, area)
        self._attr_unique_id = f"{self._hub_id}_area_{area.id}_power"

    @property
    def native_value(self) -> int | None:
        """Return the power in watts."""
        area = self._area
        return area.power if area is not None else None
