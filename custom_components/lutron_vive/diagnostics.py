"""Diagnostics download: what the hub actually answered."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

TO_REDACT = {
    "password",
    "username",
    "host",
    "fingerprint",
    "SerialNumber",
    "serial",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    hub = entry.runtime_data
    return async_redact_data(
        {
            "entry": dict(entry.data),
            "available": hub.available,
            "info": asdict(hub.info),
            "areas": [asdict(area) for area in hub.areas.values()],
            "zones": [asdict(zone) for zone in hub.zones.values()],
            "raw_responses": hub.debug_dump,
        },
        TO_REDACT,
    )
