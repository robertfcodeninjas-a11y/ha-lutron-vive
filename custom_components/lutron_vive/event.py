"""Pico remote buttons as event entities."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BUTTON_EVENT_TYPES, DOMAIN, MANUFACTURER
from .entity import ViveEntity
from .hub import ViveButton, ViveButtonDevice, ViveHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up one event entity per button."""
    hub = entry.runtime_data
    async_add_entities(
        ViveButtonEvent(hub, entry, hub.button_devices[button.device_id], button)
        for button in hub.buttons.values()
        if button.device_id in hub.button_devices
    )


def button_device_info(
    hub: ViveHub, hub_id: str, device: ViveButtonDevice
) -> DeviceInfo:
    """Device registry info for a Pico / keypad."""
    area = hub.areas.get(device.area_id) if device.area_id else None
    return DeviceInfo(
        identifiers={(DOMAIN, f"{hub_id}_device_{device.id}")},
        manufacturer=MANUFACTURER,
        model=device.model or device.device_type or "Pico",
        serial_number=device.serial,
        name=f"{area.name} {device.name}" if area else device.name,
        suggested_area=area.name if area else None,
        via_device=(DOMAIN, hub_id),
    )


class ViveButtonEvent(ViveEntity, EventEntity):
    """Fires press / release (and any other reported action) for one button."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_translation_key = "button"
    _attr_event_types = sorted(set(BUTTON_EVENT_TYPES.values()))

    def __init__(
        self,
        hub: ViveHub,
        entry: ConfigEntry,
        device: ViveButtonDevice,
        button: ViveButton,
    ) -> None:
        """Initialize the entity."""
        super().__init__(hub, entry)
        self._button_id = button.id
        self._listen_key = f"button:{button.id}"
        self._attr_unique_id = f"{self._hub_id}_button_{button.id}"
        self._attr_name = button.name
        self._attr_device_info = button_device_info(hub, self._hub_id, device)
        self._attr_extra_state_attributes = {
            "button_id": button.id,
            "button_number": button.number,
        }

    @callback
    def _updated(self) -> None:
        button = self._hub.buttons.get(self._button_id)
        event_type = BUTTON_EVENT_TYPES.get(button.last_event or "") if button else None
        if event_type is not None:
            self._trigger_event(event_type)
        self.async_write_ha_state()
