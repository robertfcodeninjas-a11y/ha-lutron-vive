"""Dimmable Vive zones as lights."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    LIGHT_CONTROL_TYPES,
    SERVICE_LOWER,
    SERVICE_RAISE,
    SERVICE_STOP,
)
from .entity import ViveZoneEntity
from .hub import CMD_LOWER, CMD_RAISE, CMD_STOP
from .leap import LeapError


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up lights."""
    hub = entry.runtime_data
    async_add_entities(
        ViveLight(hub, entry, zone)
        for zone in hub.zones.values()
        if zone.control_type in LIGHT_CONTROL_TYPES
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(SERVICE_RAISE, {}, "async_raise")
    platform.async_register_entity_service(SERVICE_LOWER, {}, "async_lower")
    platform.async_register_entity_service(SERVICE_STOP, {}, "async_stop")


class ViveLight(ViveZoneEntity, LightEntity):
    """A dimmed zone."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.TRANSITION
    _last_level: float = 100.0

    @property
    def is_on(self) -> bool | None:
        """Return True when the zone is above 0%."""
        zone = self._zone
        if zone is None or zone.level is None:
            return None
        return zone.level > 0

    @property
    def brightness(self) -> int | None:
        """Return brightness 0-255."""
        zone = self._zone
        if zone is None or zone.level is None:
            return None
        if zone.level > 0:
            self._last_level = zone.level
        return round(zone.level * 255 / 100)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, optionally to a brightness and with a fade."""
        if ATTR_BRIGHTNESS in kwargs:
            level = max(1.0, kwargs[ATTR_BRIGHTNESS] * 100 / 255)
        else:
            level = self._last_level or 100.0
        await self._set(level, kwargs.get(ATTR_TRANSITION))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off, optionally with a fade."""
        await self._set(0, kwargs.get(ATTR_TRANSITION))

    async def async_raise(self) -> None:
        """Start brightening; continues until stop (or full brightness)."""
        await self._move(CMD_RAISE)

    async def async_lower(self) -> None:
        """Start dimming; continues until stop (or the low end)."""
        await self._move(CMD_LOWER)

    async def async_stop(self) -> None:
        """Stop a raise or lower in progress."""
        await self._move(CMD_STOP)

    async def _move(self, direction: str) -> None:
        try:
            await self._hub.move(self._zone_id, direction)
        except (LeapError, KeyError) as err:
            raise HomeAssistantError(f"Vive hub command failed: {err}") from err

    async def _set(self, level: float, transition: float | None) -> None:
        try:
            await self._hub.set_level(self._zone_id, level, transition)
        except (LeapError, KeyError) as err:
            raise HomeAssistantError(f"Vive hub command failed: {err}") from err
