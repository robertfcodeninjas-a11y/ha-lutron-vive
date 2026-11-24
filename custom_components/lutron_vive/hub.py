"""Model of a Lutron Vive hub: areas, zones, live status and commands.

No Home Assistant imports here so this can run standalone (see
tools/vive_probe.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .const import (
    CONTROL_CCO,
    CONTROL_DIMMED,
    CONTROL_RECEPTACLE,
    CONTROL_SWITCHED,
)
from .leap import (
    LeapAuthError,
    LeapClient,
    LeapConnectionError,
    LeapError,
    LeapFingerprintError,
    LeapLoginError,
    LeapResponse,
)

_LOGGER = logging.getLogger(__name__)

PING_INTERVAL = 60.0
POLL_INTERVAL = 30.0
RECONNECT_MIN = 5.0
RECONNECT_MAX = 300.0

_ID_RE = re.compile(r"^/[a-z]+/(\d+)")


def href_id(href: str | None) -> str | None:
    """Return the numeric id in an href like /zone/12 or /button/7/status."""
    if not href:
        return None
    match = _ID_RE.match(href)
    return match.group(1) if match else None


@dataclass
class ViveArea:
    """An area (room / space) programmed in the hub."""

    id: str
    name: str
    parent_id: str | None = None
    is_leaf: bool = True
    occupancy: str | None = None
    power: int | None = None
    max_power: int | None = None
    level: float | None = None
    has_status: bool = False


@dataclass
class ViveZone:
    """A controllable zone (a group of fixtures on one load controller)."""

    id: str
    name: str
    control_type: str
    area_id: str | None = None
    level: float | None = None
    switched_level: str | None = None
    cco_level: str | None = None
    receptacle_level: str | None = None
    has_status: bool = False

    @property
    def is_on(self) -> bool | None:
        """Return whether the zone is on, or None when unknown."""
        if self.control_type == CONTROL_CCO and self.cco_level is not None:
            return self.cco_level == "Closed"
        if (
            self.control_type == CONTROL_RECEPTACLE
            and self.receptacle_level is not None
        ):
            return self.receptacle_level == "On"
        if self.switched_level is not None:
            return self.switched_level == "On"
        if self.level is not None:
            return self.level > 0
        return None


@dataclass
class ViveButton:
    """One button on a Pico or keypad."""

    id: str
    number: int
    name: str
    device_id: str
    last_event: str | None = None


@dataclass
class ViveButtonDevice:
    """A Pico remote or other button station."""

    id: str
    name: str
    device_type: str | None = None
    model: str | None = None
    serial: str | None = None
    area_id: str | None = None
    buttons: list[str] = field(default_factory=list)


@dataclass
class ViveHubInfo:
    """Identity of the hub."""

    serial: str | None = None
    model: str | None = None
    name: str | None = None
    firmware: str | None = None
    project_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


Listener = Callable[[], None]
ButtonEventListener = Callable[["ViveButton", "ViveButtonDevice", str], None]

# Zone commands that move a level continuously until Stop.
CMD_RAISE = "Raise"
CMD_LOWER = "Lower"
CMD_STOP = "Stop"


class ViveHub:
    """Keeps one LEAP session to a hub alive and mirrors its state."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        fingerprint: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Initialize the hub."""
        self.host = host
        self.port = port
        self._username = username
        self._password = password
        self._fingerprint = fingerprint
        self._ssl_context = ssl_context

        self.info = ViveHubInfo()
        self.areas: dict[str, ViveArea] = {}
        self.zones: dict[str, ViveZone] = {}
        self.button_devices: dict[str, ViveButtonDevice] = {}
        self.buttons: dict[str, ViveButton] = {}
        self.available = False
        self.fingerprint: str | None = fingerprint
        self.login_variant: str | None = None
        # Raw responses kept for the diagnostics download.
        self.debug_dump: dict[str, Any] = {}

        self._client: LeapClient | None = None
        self._runner: asyncio.Task[None] | None = None
        self._loaded = asyncio.Event()
        self._startup_error: Exception | None = None
        self._zone_push = False
        self._area_push = False
        self._listeners: dict[str, list[Listener]] = {}
        self._button_listeners: list[ButtonEventListener] = []
        self._button_push = False
        self.on_auth_failed: Callable[[], None] | None = None

    # --------------------------------------------------------------- listeners

    def subscribe(self, key: str, listener: Listener) -> Callable[[], None]:
        """Call listener when key changes.

        Keys: "zone:<id>", "area:<id>" and "hub" (availability).
        """
        self._listeners.setdefault(key, []).append(listener)

        def _remove() -> None:
            listeners = self._listeners.get(key, [])
            if listener in listeners:
                listeners.remove(listener)

        return _remove

    def add_button_listener(self, listener: ButtonEventListener) -> Callable[[], None]:
        """Call listener(button, device, event_type) on every button event."""
        self._button_listeners.append(listener)

        def _remove() -> None:
            if listener in self._button_listeners:
                self._button_listeners.remove(listener)

        return _remove

    def _notify(self, key: str) -> None:
        for listener in list(self._listeners.get(key, [])):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Listener for %s failed", key)

    def _notify_all(self) -> None:
        for key in list(self._listeners):
            self._notify(key)

    # ------------------------------------------------------------ life cycle

    async def async_start(self, timeout: float = 60.0) -> None:
        """Connect, load everything and keep the session alive.

        Raises LeapAuthError / LeapConnectionError when the first connection
        fails.
        """
        self._loaded.clear()
        self._startup_error = None
        self._runner = asyncio.create_task(
            self._run(), name=f"lutron_vive hub {self.host}"
        )
        try:
            async with asyncio.timeout(timeout):
                await self._loaded.wait()
        except TimeoutError as err:
            await self.async_stop()
            raise LeapConnectionError("Timed out loading data from the hub") from err
        if self._startup_error is not None:
            error = self._startup_error
            await self.async_stop()
            raise error

    async def async_stop(self) -> None:
        """Disconnect and stop reconnecting."""
        runner = self._runner
        self._runner = None
        if runner is not None:
            runner.cancel()
            try:
                await runner
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._set_available(False)

    def _set_available(self, available: bool) -> None:
        if self.available != available:
            self.available = available
            self._notify_all()

    async def _run(self) -> None:
        delay = RECONNECT_MIN
        first = True
        while True:
            client = LeapClient(
                self.host,
                self.port,
                ssl_context=self._ssl_context,
                fingerprint=self._fingerprint,
            )
            self._client = client
            try:
                await client.connect()
                self.fingerprint = client.fingerprint
                await client.login(self._username, self._password)
                self.login_variant = client.login_variant
                await self._load(client)
                self._set_available(True)
                self._notify_all()
                self._loaded.set()
                first = False
                delay = RECONNECT_MIN
                await self._hold(client)
            except asyncio.CancelledError:
                raise
            except (LeapAuthError, LeapLoginError, LeapFingerprintError) as err:
                _LOGGER.error("Vive hub %s: %s", self.host, err)
                if first:
                    self._startup_error = err
                    self._loaded.set()
                    return
                self._set_available(False)
                if isinstance(err, LeapAuthError) and self.on_auth_failed:
                    self.on_auth_failed()
                    return
            except LeapError as err:
                if first:
                    self._startup_error = err
                    self._loaded.set()
                    return
                _LOGGER.warning(
                    "Vive hub %s connection problem: %s (retrying in %.0fs)",
                    self.host,
                    err,
                    delay,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error talking to Vive hub %s", self.host)
                if first:
                    self._startup_error = LeapConnectionError(repr(err))
                    self._loaded.set()
                    return
            finally:
                await client.close()

            self._set_available(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _hold(self, client: LeapClient) -> None:
        """Keep the connection alive until it drops."""
        closed = asyncio.create_task(client.wait_closed())
        elapsed = 0.0
        try:
            while not closed.done():
                step = PING_INTERVAL if (self._zone_push and self._area_push) else POLL_INTERVAL
                await asyncio.wait({closed}, timeout=step)
                if closed.done():
                    break
                elapsed += step
                # Any answer at all, even an error status, proves the link is up.
                if elapsed >= PING_INTERVAL:
                    elapsed = 0.0
                    await client.read("/server/1/status/ping")
                if not self._zone_push:
                    await self._poll_zones(client)
                if not self._area_push:
                    await self._poll_areas(client)
        finally:
            closed.cancel()
        raise LeapConnectionError("Connection to hub lost")

    # ----------------------------------------------------------------- loading

    async def _try_read(self, client: LeapClient, url: str) -> LeapResponse | None:
        """Read a URL, returning None unless it has content."""
        response = await client.read(url)
        self.debug_dump[url] = {
            "status": f"{response.status_code} {response.status_text}",
            "body_type": response.body_type,
            "body": response.body,
        }
        return response if response.has_content else None

    async def _load(self, client: LeapClient) -> None:
        self.debug_dump = {}
        await self._load_info(client)
        areas = await self._load_areas(client)
        zones = await self._load_zones(client, areas)
        await self._load_buttons(client)

        # Carry state over a reconnect so entities do not flap to unknown.
        for zone_id, zone in zones.items():
            old = self.zones.get(zone_id)
            if old is not None:
                zone.level = old.level
                zone.switched_level = old.switched_level
                zone.cco_level = old.cco_level
                zone.receptacle_level = old.receptacle_level
        for area_id, area in areas.items():
            old_area = self.areas.get(area_id)
            if old_area is not None:
                area.occupancy = old_area.occupancy
                area.power = old_area.power
        self.areas = areas
        self.zones = zones

        await self._start_zone_status(client)
        await self._start_area_status(client)
        await self._start_button_events(client)
        _LOGGER.info(
            "Vive hub %s: %d areas, %d zones, %d button devices "
            "(zone push=%s, area push=%s, button events=%s)",
            self.host,
            len(self.areas),
            len(self.zones),
            len(self.button_devices),
            self._zone_push,
            self._area_push,
            self._button_push,
        )

    async def _load_info(self, client: LeapClient) -> None:
        info = ViveHubInfo()
        response = await self._try_read(client, "/device?where=IsThisDevice:true")
        devices = (response.body.get("Devices") if response else None) or []
        if devices:
            device = devices[0]
            info.raw = device
            serial = device.get("SerialNumber")
            info.serial = str(serial) if serial is not None else None
            info.model = device.get("ModelNumber") or device.get("DeviceType")
            info.name = device.get("Name")
            firmware = device.get("FirmwareImage") or {}
            if isinstance(firmware, dict):
                firmware = (firmware.get("Firmware") or {}).get("DisplayName")
            info.firmware = firmware if isinstance(firmware, str) else None
        response = await self._try_read(client, "/project")
        if response:
            project = response.body.get("Project") or {}
            info.project_name = project.get("Name")
            if not info.model:
                info.model = project.get("ProductType")
        self.info = info

    async def _load_areas(self, client: LeapClient) -> dict[str, ViveArea]:
        areas: dict[str, ViveArea] = {}
        response = await self._try_read(client, "/area")
        for item in (response.body.get("Areas") if response else None) or []:
            area_id = href_id(item.get("href"))
            if area_id is None:
                continue
            areas[area_id] = ViveArea(
                id=area_id,
                name=item.get("Name") or f"Area {area_id}",
                parent_id=href_id((item.get("Parent") or {}).get("href")),
                is_leaf=bool(item.get("IsLeaf", True)),
            )
        return areas

    @staticmethod
    def _parse_zone(item: dict[str, Any], area_id: str | None = None) -> ViveZone | None:
        zone_id = href_id(item.get("href"))
        if zone_id is None:
            return None
        control = item.get("ControlType")
        if not control:
            available = item.get("AvailableControlTypes") or []
            control = available[0] if available else CONTROL_DIMMED
        return ViveZone(
            id=zone_id,
            name=item.get("Name") or f"Zone {zone_id}",
            control_type=str(control),
            area_id=href_id((item.get("AssociatedArea") or {}).get("href")) or area_id,
        )

    async def _load_zones(
        self, client: LeapClient, areas: dict[str, ViveArea]
    ) -> dict[str, ViveZone]:
        zones: dict[str, ViveZone] = {}
        # Some LEAP servers list every zone at /zone ...
        response = await self._try_read(client, "/zone")
        for item in (response.body.get("Zones") if response else None) or []:
            zone = self._parse_zone(item)
            if zone is not None:
                zones[zone.id] = zone
        if zones:
            return zones
        # ... others only per area.
        for area in areas.values():
            response = await self._try_read(client, f"/area/{area.id}/associatedzone")
            for item in (response.body.get("Zones") if response else None) or []:
                zone = self._parse_zone(item, area.id)
                if zone is not None:
                    zones[zone.id] = zone
        return zones

    # ------------------------------------------------------------------ status

    def _apply_zone_status(self, status: dict[str, Any]) -> None:
        zone_id = href_id((status.get("Zone") or {}).get("href")) or href_id(
            status.get("href")
        )
        zone = self.zones.get(zone_id) if zone_id else None
        if zone is None:
            return
        if "Level" in status and status["Level"] is not None:
            zone.level = float(status["Level"])
        if status.get("SwitchedLevel"):
            zone.switched_level = status["SwitchedLevel"]
        if status.get("CCOLevel"):
            zone.cco_level = status["CCOLevel"]
        if status.get("ReceptacleLevel"):
            zone.receptacle_level = status["ReceptacleLevel"]
        zone.has_status = True
        self._notify(f"zone:{zone.id}")

    def _apply_area_status(self, status: dict[str, Any]) -> None:
        area_id = href_id(status.get("href"))
        area = self.areas.get(area_id) if area_id else None
        if area is None:
            return
        if status.get("OccupancyStatus"):
            area.occupancy = status["OccupancyStatus"]
        if status.get("InstantaneousPower") is not None:
            area.power = int(status["InstantaneousPower"])
        if status.get("InstantaneousMaxPower") is not None:
            area.max_power = int(status["InstantaneousMaxPower"])
        if status.get("Level") is not None:
            area.level = float(status["Level"])
        area.has_status = True
        self._notify(f"area:{area.id}")

    def _handle_zone_message(self, response: LeapResponse) -> None:
        body = response.body
        for status in body.get("ZoneStatuses") or []:
            self._apply_zone_status(status)
        if isinstance(body.get("ZoneStatus"), dict):
            self._apply_zone_status(body["ZoneStatus"])

    def _handle_area_message(self, response: LeapResponse) -> None:
        body = response.body
        for status in body.get("AreaStatuses") or []:
            self._apply_area_status(status)
        if isinstance(body.get("AreaStatus"), dict):
            self._apply_area_status(body["AreaStatus"])

    async def _start_zone_status(self, client: LeapClient) -> None:
        response = await client.subscribe("/zone/status", self._handle_zone_message)
        self.debug_dump["subscribe /zone/status"] = {
            "status": f"{response.status_code} {response.status_text}",
            "body": response.body,
        }
        self._zone_push = response.ok
        if response.ok:
            self._handle_zone_message(response)
        # Fill in anything the snapshot did not cover (or everything, when the
        # hub does not support the collection subscription).
        await self._poll_zones(client, only_missing=response.ok)

    async def _poll_zones(self, client: LeapClient, only_missing: bool = False) -> None:
        for zone in list(self.zones.values()):
            if only_missing and zone.has_status:
                continue
            response = await client.read(f"/zone/{zone.id}/status")
            if response.has_content:
                self._handle_zone_message(response)

    async def _start_area_status(self, client: LeapClient) -> None:
        response = await client.subscribe("/area/status", self._handle_area_message)
        self.debug_dump["subscribe /area/status"] = {
            "status": f"{response.status_code} {response.status_text}",
            "body": response.body,
        }
        if response.ok:
            self._area_push = True
            self._handle_area_message(response)
            return
        # Fall back to one subscription per area, then to polling.
        self._area_push = True
        for area in list(self.areas.values()):
            response = await client.subscribe(
                f"/area/{area.id}/status", self._handle_area_message
            )
            if response.ok:
                self._handle_area_message(response)
            else:
                self._area_push = False
                break
        if not self._area_push:
            await self._poll_areas(client)

    async def _poll_areas(self, client: LeapClient) -> None:
        for area in list(self.areas.values()):
            response = await client.read(f"/area/{area.id}/status")
            if response.has_content:
                self._handle_area_message(response)

    # ----------------------------------------------------------------- buttons

    @staticmethod
    def _button_name(item: dict[str, Any], number: int) -> str:
        engraving = item.get("Engraving") or {}
        text = engraving.get("Text") if isinstance(engraving, dict) else None
        if text and text.strip():
            return " ".join(text.split())
        return item.get("Name") or f"Button {number}"

    def _add_button(
        self,
        item: dict[str, Any],
        device: ViveButtonDevice,
        buttons: dict[str, ViveButton],
    ) -> None:
        button_id = href_id(item.get("href"))
        if button_id is None or button_id in buttons:
            return
        try:
            number = int(item.get("ButtonNumber", 0))
        except (TypeError, ValueError):
            number = 0
        buttons[button_id] = ViveButton(
            id=button_id,
            number=number,
            name=self._button_name(item, number),
            device_id=device.id,
        )
        device.buttons.append(button_id)

    async def _load_buttons(self, client: LeapClient) -> None:
        """Find Pico remotes / keypads and their buttons.

        Tries the collection routes first (/device, /buttongroup, /button)
        and falls back to the per-device expanded route. A hub that supports
        none of them simply ends up with no buttons.
        """
        devices: dict[str, ViveButtonDevice] = {}
        buttons: dict[str, ViveButton] = {}
        try:
            response = await self._try_read(client, "/device")
            raw_devices = (response.body.get("Devices") if response else None) or []
            for item in raw_devices:
                device_id = href_id(item.get("href"))
                if device_id is None or item.get("IsThisDevice"):
                    continue
                serial = item.get("SerialNumber")
                devices[device_id] = ViveButtonDevice(
                    id=device_id,
                    name=item.get("Name") or f"Device {device_id}",
                    device_type=item.get("DeviceType"),
                    model=item.get("ModelNumber"),
                    serial=str(serial) if serial is not None else None,
                    area_id=href_id((item.get("AssociatedArea") or {}).get("href")),
                )

            # Map button group -> owning device.
            group_owner: dict[str, str] = {}
            response = await self._try_read(client, "/buttongroup")
            for group in (response.body.get("ButtonGroups") if response else None) or []:
                group_id = href_id(group.get("href"))
                owner = href_id((group.get("Parent") or {}).get("href"))
                if group_id and owner in devices:
                    group_owner[group_id] = owner

            if group_owner:
                response = await self._try_read(client, "/button")
                for item in (response.body.get("Buttons") if response else None) or []:
                    group_id = href_id((item.get("Parent") or {}).get("href"))
                    owner = group_owner.get(group_id or "")
                    if owner:
                        self._add_button(item, devices[owner], buttons)

            if not buttons:
                for device in devices.values():
                    if not _looks_like_button_device(device):
                        continue
                    response = await self._try_read(
                        client, f"/device/{device.id}/buttongroup/expanded"
                    )
                    groups = (
                        response.body.get("ButtonGroupsExpanded") if response else None
                    ) or []
                    for group in groups:
                        for item in group.get("Buttons") or []:
                            self._add_button(item, device, buttons)
        except LeapConnectionError:
            raise
        except LeapError as err:
            _LOGGER.debug("Could not load buttons: %s", err)

        for device in devices.values():
            device.buttons.sort(key=lambda b: (buttons[b].number, b))
        self.button_devices = {
            device_id: device for device_id, device in devices.items() if device.buttons
        }
        self.buttons = buttons

    def _handle_button_message(self, response: LeapResponse) -> None:
        status = response.body.get("ButtonStatus")
        if not isinstance(status, dict):
            return
        button_id = href_id((status.get("Button") or {}).get("href")) or href_id(
            status.get("href")
        )
        button = self.buttons.get(button_id or "")
        event = (status.get("ButtonEvent") or {}).get("EventType")
        if button is None or not event:
            return
        button.last_event = str(event)
        device = self.button_devices.get(button.device_id)
        if device is None:
            return
        for listener in list(self._button_listeners):
            try:
                listener(button, device, button.last_event)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Button listener failed")
        self._notify(f"button:{button.id}")

    async def _start_button_events(self, client: LeapClient) -> None:
        self._button_push = False
        if not self.buttons:
            return
        self._button_push = True
        for button in list(self.buttons.values()):
            # The reply to the subscribe itself is the current state, not a
            # new press, so it is not reported as an event.
            response = await client.subscribe(
                f"/button/{button.id}/status/event", self._handle_button_message
            )
            if not response.ok:
                self.debug_dump["subscribe /button/status/event"] = {
                    "status": f"{response.status_code} {response.status_text}",
                    "body": response.body,
                }
                _LOGGER.warning(
                    "Vive hub %s does not report button events (%s %s); "
                    "Pico buttons will not trigger automations",
                    self.host,
                    response.status_code,
                    response.status_text,
                )
                self._button_push = False
                return

    # ---------------------------------------------------------------- commands

    async def _send(self, zone: ViveZone, commands: list[dict[str, Any]]) -> None:
        """Send the first command the hub accepts."""
        client = self._client
        if client is None or not client.connected:
            raise LeapConnectionError("Vive hub is not connected")
        last: LeapResponse | None = None
        for command in commands:
            last = await client.command(f"/zone/{zone.id}/commandprocessor", command)
            if last.ok:
                return
            _LOGGER.debug(
                "Hub rejected %s for zone %s: %s %s",
                command.get("CommandType"),
                zone.id,
                last.status_code,
                last.status_text,
            )
        raise LeapError(
            f"Hub rejected command for zone {zone.name}: "
            f"{last.status_code if last else '?'} {last.status_text if last else ''}"
        )

    @staticmethod
    def _duration(seconds: float) -> str:
        total = max(0, int(round(seconds)))
        hours, rest = divmod(total, 3600)
        minutes, secs = divmod(rest, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    async def set_level(
        self, zone_id: str, level: float, fade_seconds: float | None = None
    ) -> None:
        """Set a dimmed zone to level (0-100)."""
        zone = self.zones[zone_id]
        level = max(0, min(100, int(round(level))))
        params: dict[str, Any] = {"Level": level}
        if fade_seconds is not None:
            params["FadeTime"] = self._duration(fade_seconds)
        await self._send(
            zone,
            [
                {"CommandType": "GoToDimmedLevel", "DimmedLevelParameters": params},
                {
                    "CommandType": "GoToLevel",
                    "Parameter": [{"Type": "Level", "Value": level}],
                },
            ],
        )
        zone.level = float(level)
        self._notify(f"zone:{zone.id}")

    async def move(self, zone_id: str, direction: str) -> None:
        """Start raising / lowering a zone, or stop it (Raise / Lower / Stop)."""
        if direction not in (CMD_RAISE, CMD_LOWER, CMD_STOP):
            raise ValueError(f"Unknown direction {direction}")
        zone = self.zones[zone_id]
        await self._send(zone, [{"CommandType": direction}])

    async def set_on(self, zone_id: str, on: bool) -> None:
        """Turn a switched / receptacle / CCO zone on or off."""
        zone = self.zones[zone_id]
        state = "On" if on else "Off"
        switched = {
            "CommandType": "GoToSwitchedLevel",
            "SwitchedLevelParameters": {"SwitchedLevel": state},
        }
        generic = {
            "CommandType": "GoToLevel",
            "Parameter": [{"Type": "Level", "Value": 100 if on else 0}],
        }
        if zone.control_type == CONTROL_CCO:
            cco = "Closed" if on else "Open"
            await self._send(
                zone,
                [
                    {
                        "CommandType": "GoToCCOLevel",
                        "CCOLevelParameters": {"CCOLevel": cco},
                    },
                    generic,
                ],
            )
            zone.cco_level = cco
        elif zone.control_type == CONTROL_RECEPTACLE:
            await self._send(
                zone,
                [
                    {
                        "CommandType": "GoToReceptacleLevel",
                        "ReceptacleLevelParameters": {"ReceptacleLevel": state},
                    },
                    switched,
                    generic,
                ],
            )
            zone.receptacle_level = state
        elif zone.control_type == CONTROL_SWITCHED:
            await self._send(zone, [switched, generic])
            zone.switched_level = state
        else:
            await self.set_level(zone_id, 100 if on else 0)
            return
        zone.level = 100.0 if on else 0.0
        self._notify(f"zone:{zone.id}")


def _looks_like_button_device(device: ViveButtonDevice) -> bool:
    text = f"{device.device_type or ''} {device.model or ''}".lower()
    return any(word in text for word in ("pico", "keypad", "remote", "pj2", "pj"))


async def async_probe(
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    fingerprint: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> tuple[ViveHubInfo, str | None]:
    """Connect and log in once; return hub identity and TLS fingerprint.

    Used by the config flow to validate what the user typed.
    """
    client = LeapClient(host, port, ssl_context=ssl_context, fingerprint=fingerprint)
    hub = ViveHub(host, port, username, password)
    try:
        await client.connect()
        await client.login(username, password)
        await hub._load_info(client)  # noqa: SLF001
    finally:
        await client.close()
    return hub.info, client.fingerprint
