"""Lutron Vive integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import (
    BUTTON_EVENT_TYPES,
    CONF_FINGERPRINT,
    DEFAULT_PORT,
    DOMAIN,
    EVENT_BUTTON,
    MANUFACTURER,
)
from .hub import ViveButton, ViveButtonDevice, ViveHub
from .leap import (
    LeapAuthError,
    LeapError,
    LeapFingerprintError,
    build_ssl_context,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type ViveConfigEntry = ConfigEntry[ViveHub]


def hub_identifier(entry: ConfigEntry) -> str:
    """Return the stable identifier used for the hub and its entities."""
    return entry.unique_id or entry.entry_id


async def async_setup_entry(hass: HomeAssistant, entry: ViveConfigEntry) -> bool:
    """Set up a Vive hub from a config entry."""
    ssl_context = await hass.async_add_executor_job(build_ssl_context)
    hub = ViveHub(
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        fingerprint=entry.data.get(CONF_FINGERPRINT),
        ssl_context=ssl_context,
    )

    @callback
    def _auth_failed() -> None:
        entry.async_start_reauth(hass)

    hub.on_auth_failed = _auth_failed

    try:
        await hub.async_start()
    except LeapAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except LeapFingerprintError as err:
        raise ConfigEntryNotReady(
            f"{err}. If the hub was replaced or factory reset, remove and "
            "re-add the integration."
        ) from err
    except LeapError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = hub

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, hub_identifier(entry))},
        manufacturer=MANUFACTURER,
        name=hub.info.name or hub.info.project_name or entry.title,
        model=hub.info.model or "Vive hub",
        sw_version=hub.info.firmware,
        serial_number=hub.info.serial,
        configuration_url=f"https://{hub.host}",
    )

    hub_id = hub_identifier(entry)

    @callback
    def _button_event(
        button: ViveButton, device: ViveButtonDevice, event_type: str
    ) -> None:
        area = hub.areas.get(device.area_id) if device.area_id else None
        device_entry = dr.async_get(hass).async_get_device(
            identifiers={(DOMAIN, f"{hub_id}_device_{device.id}")}
        )
        hass.bus.async_fire(
            EVENT_BUTTON,
            {
                "device_id": device_entry.id if device_entry else None,
                "hub": hub_id,
                "remote_id": device.id,
                "remote_name": device.name,
                "area_name": area.name if area else None,
                "button_id": button.id,
                "button_number": button.number,
                "button_name": button.name,
                "action": BUTTON_EVENT_TYPES.get(event_type, event_type.lower()),
                "leap_event_type": event_type,
            },
        )

    entry.async_on_unload(hub.add_button_listener(_button_event))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ViveConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
