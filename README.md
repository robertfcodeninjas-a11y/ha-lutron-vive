# Lutron Vive for Home Assistant

A custom integration that connects Home Assistant to a Lutron Vive hub over
the hub's local API (LEAP, JSON over TLS on port 8081). Local push, no cloud.

Tested on a real Vive hub for login, zones, dimming and switching. Raise /
lower / stop, Pico button events and network discovery are new in 0.2.0 and so
far only tested against the simulated hub in `tools/mock_hub.py`. Reports
welcome.

## What you get

| Vive | Home Assistant |
| --- | --- |
| Dimmed zones | `light` with brightness and transition (fade), plus `lutron_vive.raise` / `lower` / `stop` |
| Switched zones | `switch` |
| Receptacle zones | `switch` (outlet) |
| Contact-closure (CCO) zones | `switch` (on = closed) |
| Pico remotes | one `event` entity per button, and a `lutron_vive_button_event` event |
| Area occupancy | `binary_sensor` (occupancy), for areas that report it |
| Area power | `sensor` in watts, for areas that report it |

Changes made at wall controls, Picos, sensors and timeclocks show up in Home
Assistant right away. If a hub does not push zone or area changes, the
integration polls them every 30 s instead.

## Hub requirements

- A Vive hub with API support: HJS-2 (and HMS-2 / HKS-2) on firmware 1.12 or
  newer. Older `-0` / `-1` hubs need Lutron's HJS-UPDATE license.
- The hub connected to your network by **Ethernet** (the API does not work
  over the hub's Wi-Fi).
- An API integration on the hub: in the Vive app or hub web page, go to
  **More > API > Add**, choose **Login with a username / password**, and set a
  username and password. These are what you enter in Home Assistant; they are
  not your Vive app or hub admin login.
- Give the hub a fixed IP address (DHCP reservation). Discovery will also
  update the address if it changes.

## Install

### From the Home Assistant Marketplace (2026.11 and newer)

1. Settings > Marketplace > **Add from link**.
2. Paste `https://github.com/robertfcodeninjas-a11y/ha-lutron-vive` and install **Lutron Vive**.
3. Settings > Devices & services. The hub usually shows up as **Discovered**;
   otherwise **Add integration > Lutron Vive** and enter its IP address.
4. Enter the API integration username and password.

### With HACS (older Home Assistant)

HACS > ⋮ > Custom repositories > add `https://github.com/robertfcodeninjas-a11y/ha-lutron-vive`, category
**Integration**, then install, restart and add the integration as above.

### By hand

Copy `custom_components/lutron_vive` into the `custom_components` folder of
your Home Assistant config directory, restart, and add the integration.

## Using it

### Raise / lower / stop

Start a continuous dim up or down, then stop it, for example from a
press-and-hold button:

```yaml
action: lutron_vive.raise
target:
  entity_id: light.main_bay_overheads
```

Call `lutron_vive.stop` on release. Without a stop, the light keeps going until
it reaches full brightness or the low end.

### Pico remotes

Each Pico button becomes an event entity, such as `event.office_pico_on`,
whose `event_type` is `press` or `release` (and `hold`, `double_press` or
`multi_press` if the hub reports those). Use it as an automation trigger:

```yaml
triggers:
  - trigger: state
    entity_id: event.office_pico_on
    attribute: event_type
    to: press
actions:
  - action: light.turn_on
    target:
      entity_id: light.office_desk
```

Every press is also fired on the event bus as `lutron_vive_button_event`,
with `device_id`, `remote_name`, `area_name`, `button_name`, `button_number`
and `action`:

```yaml
triggers:
  - trigger: event
    event_type: lutron_vive_button_event
    event_data:
      remote_name: Pico
      button_number: 2
      action: press
```

Buttons are named from their engraving on the hub when there is one,
otherwise "Button N". Picos that are only paired directly to a load
controller, and not added to the hub, are not visible to the hub and so not
to Home Assistant.

## Try it from the command line

`tools/vive_probe.py` talks to the hub without Home Assistant (Python 3.11+,
no other packages):

```
python3 tools/vive_probe.py 192.168.1.50 USERNAME PASSWORD
python3 tools/vive_probe.py 192.168.1.50 USERNAME PASSWORD --zone 10 --level 50
python3 tools/vive_probe.py 192.168.1.50 USERNAME PASSWORD --watch
python3 tools/vive_probe.py 192.168.1.50 USERNAME PASSWORD --dump -v
```

## Troubleshooting

- **Cannot connect**: check the IP address, that the hub is on Ethernet, and
  that port 8081 is reachable from Home Assistant.
- **Hub refused the username / password**: use the API integration's
  credentials, and check that the integration finished setting up on the hub.
- **Hub did not accept the login request**: the hub answered with something
  this integration does not handle yet. Open an issue with the "Hub reply"
  text from the error.
- **Something missing or wrong**: download diagnostics from the integration's
  page (⋮ > Download diagnostics; credentials and serial numbers are
  removed), or turn on debug logging:

  ```yaml
  logger:
    logs:
      custom_components.lutron_vive: debug
  ```

## Security

The hub's TLS certificate is issued by a private Lutron CA for a name that
never matches a LAN address, so it cannot be checked the usual way. The
integration records the certificate's SHA-256 fingerprint at setup and refuses
to connect if it changes. After replacing or factory-resetting the hub,
remove and re-add the integration.

## Not implemented

- One-time-pairing (certificate) authentication
- Zone Type Group control (all zones of one type in an area at once)
- Area scenes

## Development

```
pip install -r requirements_test.txt
pytest
python3 tools/mock_hub.py --port 8081     # simulated hub, login ha / secret
```

The protocol details come from open-source LEAP work on other Lutron systems
([pylutron-caseta](https://github.com/gurumitts/pylutron-caseta),
[lutron-protocols](https://github.com/alxgmpr/lutron-protocols)) and from what
real Vive hubs answer. Not affiliated with or endorsed by Lutron.

## License

MIT
