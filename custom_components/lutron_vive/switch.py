"""Switched, receptacle and contact-closure Vive zones as switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONTROL_RECEPTACLE, LIGHT_CONTROL_TYPES
from .entity import ViveZoneEntity
from .hub import ViveHub, ViveZone
from .leap import LeapError


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up switches.

    Anything that is not a dimmed zone becomes a switch, so an unexpected
    control type still shows up and can be toggled.
    """
    hub = entry.runtime_data
    async_add_entities(
        ViveSwitch(hub, entry, zone)
        for zone in hub.zones.values()
        if zone.control_type not in LIGHT_CONTROL_TYPES
    )


class ViveSwitch(ViveZoneEntity, SwitchEntity):
    """An on/off zone."""

    def __init__(self, hub: ViveHub, entry: ConfigEntry, zone: ViveZone) -> None:
        """Initialize the switch."""
        super().__init__(hub, entry, zone)
        self._attr_device_class = (
            SwitchDeviceClass.OUTLET
            if zone.control_type == CONTROL_RECEPTACLE
            else SwitchDeviceClass.SWITCH
        )

    @property
    def is_on(self) -> bool | None:
        """Return True when the zone is on (or the contact is closed)."""
        zone = self._zone
        return zone.is_on if zone is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the zone on."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the zone off."""
        await self._set(False)

    async def _set(self, on: bool) -> None:
        try:
            await self._hub.set_on(self._zone_id, on)
        except (LeapError, KeyError) as err:
            raise HomeAssistantError(f"Vive hub command failed: {err}") from err
