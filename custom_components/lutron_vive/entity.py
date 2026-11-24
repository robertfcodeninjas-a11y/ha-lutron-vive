"""Base entities for Lutron Vive."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import hub_identifier
from .const import DOMAIN, MANUFACTURER
from .hub import ViveArea, ViveHub, ViveZone


class ViveEntity(Entity):
    """Common behaviour: push updates and availability."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _listen_key: str

    def __init__(self, hub: ViveHub, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        self._hub = hub
        self._hub_id = hub_identifier(entry)

    @property
    def available(self) -> bool:
        """Return True while the hub is connected."""
        return self._hub.available

    async def async_added_to_hass(self) -> None:
        """Register for updates."""
        self.async_on_remove(self._hub.subscribe(self._listen_key, self._updated))
        self.async_on_remove(self._hub.subscribe("hub", self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()


class ViveZoneEntity(ViveEntity):
    """An entity backed by one zone. The zone is its own device."""

    _attr_name = None

    def __init__(self, hub: ViveHub, entry: ConfigEntry, zone: ViveZone) -> None:
        """Initialize the entity."""
        super().__init__(hub, entry)
        self._zone_id = zone.id
        self._listen_key = f"zone:{zone.id}"
        self._attr_unique_id = f"{self._hub_id}_zone_{zone.id}"
        area = hub.areas.get(zone.area_id) if zone.area_id else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._hub_id}_zone_{zone.id}")},
            manufacturer=MANUFACTURER,
            model=f"Vive {zone.control_type} zone",
            name=f"{area.name} {zone.name}" if area else zone.name,
            suggested_area=area.name if area else None,
            via_device=(DOMAIN, self._hub_id),
        )

    @property
    def _zone(self) -> ViveZone | None:
        return self._hub.zones.get(self._zone_id)

    @property
    def available(self) -> bool:
        """Return True while the hub is connected and still has the zone."""
        return self._hub.available and self._zone is not None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose the LEAP zone id for troubleshooting."""
        return {"zone_id": self._zone_id}


class ViveAreaEntity(ViveEntity):
    """An entity backed by one area. The area is its own device."""

    def __init__(self, hub: ViveHub, entry: ConfigEntry, area: ViveArea) -> None:
        """Initialize the entity."""
        super().__init__(hub, entry)
        self._area_id = area.id
        self._listen_key = f"area:{area.id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._hub_id}_area_{area.id}")},
            manufacturer=MANUFACTURER,
            model="Vive area",
            name=f"{area.name} area",
            suggested_area=area.name,
            via_device=(DOMAIN, self._hub_id),
        )

    @property
    def _area(self) -> ViveArea | None:
        return self._hub.areas.get(self._area_id)

    @property
    def available(self) -> bool:
        """Return True while the hub is connected and still has the area."""
        return self._hub.available and self._area is not None
