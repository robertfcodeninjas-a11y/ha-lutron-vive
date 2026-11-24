"""End-to-end tests inside Home Assistant against the simulated hub."""

import asyncio

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lutron_vive.const import CONF_FINGERPRINT, DOMAIN


async def _settle(hass, predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, mock_hub):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "127.0.0.1",
            CONF_PORT: mock_hub.port,
            CONF_USERNAME: "ha",
            CONF_PASSWORD: "secret",
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    return result["result"]


async def test_flow_errors(hass: HomeAssistant, mock_hub):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: mock_hub.port,
         CONF_USERNAME: "ha", CONF_PASSWORD: "wrong"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    port = mock_hub.port
    await mock_hub.stop()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: port,
         CONF_USERNAME: "ha", CONF_PASSWORD: "secret"},
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_full_setup(hass: HomeAssistant, mock_hub):
    entry = await _setup(hass, mock_hub)
    assert entry.unique_id == "12345678"
    assert len(entry.data[CONF_FINGERPRINT]) == 64
    assert entry.state is config_entries.ConfigEntryState.LOADED

    states = {s.entity_id: s for s in hass.states.async_all()}
    print(sorted(states))

    light = hass.states.get("light.main_bay_overheads")
    assert light.state == "on"
    assert light.attributes["brightness"] == round(75 * 255 / 100)
    assert hass.states.get("light.main_bay_bench_lights").state == "off"
    assert hass.states.get("switch.main_bay_exhaust_fan").state == "off"
    assert hass.states.get("switch.office_outlets").state == "on"
    assert hass.states.get("switch.office_door_relay").state == "off"
    assert hass.states.get("binary_sensor.main_bay_area_occupancy").state == "on"
    assert hass.states.get("binary_sensor.office_area_occupancy").state == "off"
    assert hass.states.get("sensor.main_bay_area_power").state == "840"

    # Home Assistant -> hub
    await hass.services.async_call(
        "light", "turn_on",
        {"entity_id": "light.main_bay_bench_lights", "brightness": 128, "transition": 2},
        blocking=True,
    )
    assert mock_hub.zone_state["11"]["Level"] == 50
    assert mock_hub.commands[-1][1]["DimmedLevelParameters"]["FadeTime"] == "00:00:02"
    assert hass.states.get("light.main_bay_bench_lights").state == "on"

    await hass.services.async_call(
        "light", "turn_off", {"entity_id": "light.main_bay_overheads"}, blocking=True
    )
    assert mock_hub.zone_state["10"]["Level"] == 0
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.main_bay_overheads"}, blocking=True
    )
    assert mock_hub.zone_state["10"]["Level"] == 75  # restores last level

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.office_door_relay"}, blocking=True
    )
    assert mock_hub.zone_state["14"]["CCOLevel"] == "Closed"
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.office_outlets"}, blocking=True
    )
    assert mock_hub.zone_state["13"]["ReceptacleLevel"] == "Off"

    # hub -> Home Assistant (someone uses a wall control / sensor trips)
    mock_hub.set_zone("12", SwitchedLevel="On", Level=100)
    await _settle(hass, lambda: hass.states.get("switch.main_bay_exhaust_fan").state == "on")
    mock_hub.set_area("3", OccupancyStatus="Occupied", InstantaneousPower=455)
    await _settle(
        hass, lambda: hass.states.get("binary_sensor.office_area_occupancy").state == "on"
    )
    assert hass.states.get("sensor.office_area_power").state == "455"

    # registries
    devices = dr.async_get(hass)
    hub_device = devices.async_get_device(identifiers={(DOMAIN, "12345678")})
    assert hub_device.model == "HJS-2-FM"
    assert hub_device.sw_version == "01.14.05"
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 14  # 9 zones/areas + 5 Pico buttons

    # hub goes away -> unavailable
    mock_hub.drop_clients()
    await _settle(
        hass, lambda: hass.states.get("light.main_bay_overheads").state == "unavailable"
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_diagnostics_redacted(hass: HomeAssistant, mock_hub):
    from custom_components.lutron_vive.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await _setup(hass, mock_hub)
    data = await async_get_config_entry_diagnostics(hass, entry)
    text = str(data)
    assert "secret" not in text
    assert "12345678" not in text
    assert len(data["zones"]) == 5
    assert "/area" in data["raw_responses"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_duplicate_aborts(hass: HomeAssistant, mock_hub):
    entry = await _setup(hass, mock_hub)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: mock_hub.port,
         CONF_USERNAME: "ha", CONF_PASSWORD: "secret"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reauth(hass: HomeAssistant, mock_hub):
    entry = await _setup(hass, mock_hub)
    mock_hub.password = "rotated"
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    result = flows[0]
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "ha", CONF_PASSWORD: "still-wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "ha", CONF_PASSWORD: "rotated"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "rotated"
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert hass.states.get("light.main_bay_overheads").state == "on"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_flow_shows_hub_reply(hass: HomeAssistant, mock_hub):
    mock_hub.login_context = "SomethingElse"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: mock_hub.port,
         CONF_USERNAME: "  ha ", CONF_PASSWORD: "secret"},
    )
    assert result["errors"] == {"base": "login_failed"}
    assert "400 BadRequest" in result["description_placeholders"]["detail"]

    mock_hub.login_context = "Application"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: mock_hub.port,
         CONF_USERNAME: "  ha ", CONF_PASSWORD: "secret"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USERNAME] == "ha"
    assert await hass.config_entries.async_unload(result["result"].entry_id)


async def test_raise_lower_stop_services(hass: HomeAssistant, mock_hub):
    entry = await _setup(hass, mock_hub)
    for service, command in (("raise", "Raise"), ("lower", "Lower"), ("stop", "Stop")):
        await hass.services.async_call(
            DOMAIN, service, {"entity_id": "light.main_bay_overheads"}, blocking=True
        )
        assert mock_hub.commands[-1] == ("10", {"CommandType": command})
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pico_events(hass: HomeAssistant, mock_hub):
    entry = await _setup(hass, mock_hub)
    fired = []
    hass.bus.async_listen("lutron_vive_button_event", lambda e: fired.append(e.data))

    state = hass.states.get("event.main_bay_pico_on")
    assert state is not None, sorted(hass.states.async_entity_ids("event"))
    assert state.state == "unknown"
    assert "press" in state.attributes["event_types"]

    mock_hub.press("101", "Press")
    await _settle(hass, lambda: len(fired) == 1)
    state = hass.states.get("event.main_bay_pico_on")
    assert state.attributes["event_type"] == "press"
    data = fired[0]
    assert data["button_name"] == "On"
    assert data["button_number"] == 2
    assert data["action"] == "press"
    assert data["area_name"] == "Main Bay"
    device = dr.async_get(hass).async_get(data["device_id"])
    assert device.model == "PJ2-3BRL-GWH-L01"
    assert device.serial_number == "55501"

    mock_hub.press("105", "Release")
    await _settle(hass, lambda: len(fired) == 2)
    assert hass.states.get("event.main_bay_pico_off").attributes["event_type"] == "release"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    mock_hub.press("101", "Press")
    await asyncio.sleep(0.1)
    assert len(fired) == 2  # listener removed on unload


def _zeroconf(host="127.0.0.1", props=None, name="Vive-ABC._lutron-vive-v2._tcp.local."):
    from ipaddress import ip_address

    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        hostname="vive-abc.local.",
        name=name,
        port=80,
        type="_lutron-vive-v2._tcp.local.",
        properties=props if props is not None else {"NAME": "Shop Hub", "SN": "12345678"},
    )


async def test_zeroconf_discovery(hass: HomeAssistant, mock_hub, monkeypatch):
    from custom_components.lutron_vive import config_flow

    monkeypatch.setattr(config_flow, "DEFAULT_PORT", mock_hub.port)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"]["name"] == "Shop Hub"

    # second advertisement of the same hub does not open another prompt
    dup = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf(props={}, name="Vive-ABC._lutron-vive-v1._tcp.local."),
    )
    assert dup["type"] is FlowResultType.ABORT

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "ha", CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "ha", CONF_PASSWORD: "secret"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == "12345678"
    assert entry.data[CONF_HOST] == "127.0.0.1"

    # rediscovered at the same address: nothing to do
    again = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf()
    )
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_configured"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # hub moved to a new address: entry follows it
    moved = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf(host="127.0.0.2"),
    )
    assert moved["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "127.0.0.2"


async def test_zeroconf_ipv6_only(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf(host="fe80::1"),
    )
    assert result["reason"] == "not_ipv4"
