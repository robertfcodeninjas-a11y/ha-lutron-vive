"""Constants for the Lutron Vive integration."""

from __future__ import annotations

DOMAIN = "lutron_vive"
MANUFACTURER = "Lutron"

DEFAULT_PORT = 8081

CONF_FINGERPRINT = "fingerprint"

# LEAP zone control types exposed by a Vive hub.
CONTROL_DIMMED = "Dimmed"
CONTROL_SWITCHED = "Switched"
CONTROL_CCO = "CCO"
CONTROL_RECEPTACLE = "Receptacle"

LIGHT_CONTROL_TYPES = {CONTROL_DIMMED}
SWITCH_CONTROL_TYPES = {CONTROL_SWITCHED, CONTROL_CCO, CONTROL_RECEPTACLE}

OCCUPIED = "Occupied"
UNOCCUPIED = "Unoccupied"

SERVICE_RAISE = "raise"
SERVICE_LOWER = "lower"
SERVICE_STOP = "stop"

EVENT_BUTTON = f"{DOMAIN}_button_event"

# LEAP button EventType -> Home Assistant event type.
BUTTON_EVENT_TYPES: dict[str, str] = {
    "Press": "press",
    "Release": "release",
    "Hold": "hold",
    "LongHold": "hold",
    "DoubleTap": "double_press",
    "MultiTap": "multi_press",
}
