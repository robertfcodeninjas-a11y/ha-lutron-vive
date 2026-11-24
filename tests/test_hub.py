"""Protocol-level tests against the simulated hub (no HA entities)."""

import asyncio

import pytest

from custom_components.lutron_vive import hub as hub_module
from custom_components.lutron_vive.hub import ViveHub
from custom_components.lutron_vive.leap import (
    LeapAuthError,
    LeapConnectionError,
    LeapFingerprintError,
    LeapLoginError,
)


async def _wait(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_load_and_push(mock_hub):
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    try:
        assert hub.available
        assert hub.info.serial == "12345678"
        assert hub.info.firmware == "01.14.05"
        assert set(hub.zones) == {"10", "11", "12", "13", "14"}
        assert hub.zones["10"].level == 75
        assert hub.zones["10"].area_id == "2"
        assert hub.zones["13"].is_on is True
        assert hub.zones["14"].is_on is False
        assert hub.areas["2"].occupancy == "Occupied"
        assert hub.areas["2"].power == 840

        seen = []
        hub.subscribe("zone:11", lambda: seen.append(hub.zones["11"].level))
        mock_hub.set_zone("11", Level=33)
        await _wait(lambda: hub.zones["11"].level == 33)
        assert seen[-1] == 33

        mock_hub.set_area("3", OccupancyStatus="Occupied")
        await _wait(lambda: hub.areas["3"].occupancy == "Occupied")
        assert hub.areas["3"].power == 12  # delta did not wipe other fields
    finally:
        await hub.async_stop()
    assert not hub.available


async def test_commands(mock_hub):
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    try:
        await hub.set_level("10", 42, fade_seconds=3)
        assert mock_hub.commands[-1] == (
            "10",
            {
                "CommandType": "GoToDimmedLevel",
                "DimmedLevelParameters": {"Level": 42, "FadeTime": "00:00:03"},
            },
        )
        assert mock_hub.zone_state["10"]["Level"] == 42

        await hub.set_on("12", True)
        assert mock_hub.zone_state["12"]["SwitchedLevel"] == "On"
        await hub.set_on("13", False)
        assert mock_hub.zone_state["13"]["ReceptacleLevel"] == "Off"
        await hub.set_on("14", True)
        assert mock_hub.zone_state["14"]["CCOLevel"] == "Closed"
        assert hub.zones["14"].is_on is True
    finally:
        await hub.async_stop()


async def test_fallbacks_without_collections(mock_hub):
    """Hub with no /zone list and no collection subscriptions."""
    mock_hub.flat_zone_list = False
    mock_hub.collection_subscriptions = False
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    try:
        assert set(hub.zones) == {"10", "11", "12", "13", "14"}
        assert hub.zones["10"].level == 75
        assert hub.areas["2"].occupancy == "Occupied"
        # per-area subscriptions still push
        mock_hub.set_area("2", OccupancyStatus="Unoccupied")
        await _wait(lambda: hub.areas["2"].occupancy == "Unoccupied")
    finally:
        await hub.async_stop()


async def test_zone_polling_when_no_push(mock_hub, monkeypatch):
    monkeypatch.setattr(hub_module, "POLL_INTERVAL", 0.1)
    mock_hub.collection_subscriptions = False
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    try:
        mock_hub.zone_state["11"]["Level"] = 66  # changed with no push
        await _wait(lambda: hub.zones["11"].level == 66)
    finally:
        await hub.async_stop()


async def test_bad_password(mock_hub):
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "nope")
    with pytest.raises(LeapAuthError):
        await hub.async_start()


async def test_unreachable(socket_enabled, unused_tcp_port):
    hub = ViveHub("127.0.0.1", unused_tcp_port, "ha", "secret")
    with pytest.raises(LeapConnectionError):
        await hub.async_start()


async def test_fingerprint_pinning(mock_hub):
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret", fingerprint="00" * 32)
    with pytest.raises(LeapFingerprintError):
        await hub.async_start()

    good = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await good.async_start()
    fingerprint = good.fingerprint
    await good.async_stop()
    pinned = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret", fingerprint=fingerprint)
    await pinned.async_start()
    await pinned.async_stop()


async def test_reconnect(mock_hub, monkeypatch):
    monkeypatch.setattr(hub_module, "RECONNECT_MIN", 0.1)
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    try:
        mock_hub.drop_clients()
        await _wait(lambda: not hub.available)
        mock_hub.zone_state["10"]["Level"] = 5
        await _wait(lambda: hub.available)
        assert hub.zones["10"].level == 5
        await hub.set_level("10", 80)
        assert mock_hub.zone_state["10"]["Level"] == 80
    finally:
        await hub.async_stop()


@pytest.mark.parametrize(
    ("key", "context"),
    [("LoginID", "Application"), ("LoginId", "Application"),
     ("LoginID", None), ("LoginId", None)],
)
async def test_login_variants(mock_hub, key, context):
    mock_hub.login_key = key
    mock_hub.login_context = context
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    assert hub.available
    await hub.async_stop()


async def test_login_format_unknown_is_not_reported_as_bad_password(mock_hub):
    mock_hub.login_context = "SomethingElse"
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    with pytest.raises(LeapLoginError) as err:
        await hub.async_start()
    assert not isinstance(err.value, LeapAuthError)
    assert "400 BadRequest - This request is not supported" in str(err.value)


async def test_wrong_password_with_unusual_format(mock_hub):
    """401 on the format the hub understands, 400 on the others: that is a
    wrong password, and the hub's reply shows in the message."""
    mock_hub.login_context = None
    mock_hub.login_key = "LoginId"
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "nope")
    with pytest.raises(LeapAuthError) as err:
        await hub.async_start()
    assert "401 Unauthorized" in str(err.value)


async def test_client_setting_sent_first(mock_hub):
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    assert hub.available
    await hub.async_stop()


async def test_client_setting_version_fallback(mock_hub):
    mock_hub.accepted_major_versions = (3,)
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    assert hub.available
    await hub.async_stop()


async def test_hub_without_client_setting_requirement(mock_hub):
    mock_hub.require_client_setting = False
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    assert hub.available
    await hub.async_stop()


async def test_client_setting_refused_reported(mock_hub):
    mock_hub.accepted_major_versions = (99,)
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    with pytest.raises(LeapLoginError) as err:
        await hub.async_start()
    assert "clientsetting: v1: 400 BadRequest - Unsupported client version" in str(err.value)


async def test_raise_lower_stop(mock_hub):
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    try:
        for direction in ("Raise", "Lower", "Stop"):
            await hub.move("10", direction)
            assert mock_hub.commands[-1] == ("10", {"CommandType": direction})
        with pytest.raises(ValueError):
            await hub.move("10", "Sideways")
    finally:
        await hub.async_stop()


@pytest.mark.parametrize("mode", ["collection", "expanded"])
async def test_pico_buttons(mock_hub, mode):
    mock_hub.button_mode = mode
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    events = []
    hub.add_button_listener(lambda b, d, e: events.append((d.name, b.name, b.number, e)))
    await hub.async_start()
    try:
        assert list(hub.button_devices) == ["20"]  # sensor has no buttons
        pico = hub.button_devices["20"]
        assert pico.area_id == "2"
        assert [hub.buttons[b].name for b in pico.buttons] == [
            "On", "Button 3", "Favorite", "Button 5", "Off"]
        assert events == []  # subscribing is not a press
        mock_hub.press("101", "Press")
        mock_hub.press("101", "Release")
        await _wait(lambda: len(events) == 2)
        assert events == [("Pico", "On", 2, "Press"), ("Pico", "On", 2, "Release")]
    finally:
        await hub.async_stop()


async def test_no_buttons(mock_hub):
    mock_hub.button_mode = "none"
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    assert hub.button_devices == {} and hub.available
    await hub.async_stop()


async def test_button_events_unsupported(mock_hub):
    mock_hub.button_events_supported = False
    hub = ViveHub("127.0.0.1", mock_hub.port, "ha", "secret")
    await hub.async_start()
    assert hub.available and hub.buttons  # buttons known, just no events
    await hub.async_stop()
