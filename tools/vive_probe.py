#!/usr/bin/env python3
"""Talk to a Vive hub from the command line, without Home Assistant.

Use this first to confirm the hub's API works with your credentials:

    python3 tools/vive_probe.py 192.168.1.50 USERNAME PASSWORD
    python3 tools/vive_probe.py 192.168.1.50 USERNAME PASSWORD --dump
    python3 tools/vive_probe.py 192.168.1.50 USERNAME PASSWORD --zone 10 --level 50
    python3 tools/vive_probe.py 192.168.1.50 USERNAME PASSWORD --watch

Needs Python 3.11+ and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import types
from pathlib import Path

# Import the integration's protocol code without importing Home Assistant.
_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "lutron_vive"
_pkg = types.ModuleType("lutron_vive_standalone")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules["lutron_vive_standalone"] = _pkg

from lutron_vive_standalone.hub import ViveHub  # noqa: E402
from lutron_vive_standalone.leap import LeapError  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Probe a Lutron Vive hub over LEAP")
    parser.add_argument("host")
    parser.add_argument("username")
    parser.add_argument("password")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--dump", action="store_true", help="print raw hub responses")
    parser.add_argument("--zone", help="zone id to control")
    parser.add_argument("--level", type=float, help="dim level 0-100 for --zone")
    parser.add_argument("--on", action="store_true", help="turn --zone on")
    parser.add_argument("--off", action="store_true", help="turn --zone off")
    parser.add_argument("--watch", action="store_true", help="print live changes")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every message")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    hub = ViveHub(args.host, args.port, args.username, args.password)
    try:
        await hub.async_start()
    except LeapError as err:
        print(f"FAILED: {type(err).__name__}: {err}")
        return 1

    try:
        info = hub.info
        print(f"Connected. Hub: {info.name or '?'}  model: {info.model or '?'}  "
              f"serial: {info.serial or '?'}  firmware: {info.firmware or '?'}")
        print(f"TLS certificate SHA-256: {hub.fingerprint}")
        print(f"Login format accepted: {hub.login_variant}  "
              f"client version: {hub._client.client_version if hub._client else '?'}")
        print(f"\nAreas ({len(hub.areas)}):")
        for area in hub.areas.values():
            print(f"  [{area.id}] {area.name}  occupancy={area.occupancy}  power={area.power}")
        print(f"\nZones ({len(hub.zones)}):")
        for zone in hub.zones.values():
            area = hub.areas.get(zone.area_id or "")
            print(f"  [{zone.id}] {zone.name}  type={zone.control_type}  "
                  f"area={area.name if area else '?'}  level={zone.level}  on={zone.is_on}")

        if args.dump:
            print("\nRaw responses:")
            print(json.dumps(hub.debug_dump, indent=2, default=str))

        if args.zone:
            if args.zone not in hub.zones:
                print(f"\nNo zone with id {args.zone}")
                return 1
            if args.level is not None:
                await hub.set_level(args.zone, args.level)
            elif args.on or args.off:
                await hub.set_on(args.zone, args.on)
            print(f"\nCommand accepted for zone {args.zone}")

        if args.watch:
            print("\nWatching for changes, Ctrl+C to stop...")
            for zone in hub.zones.values():
                hub.subscribe(f"zone:{zone.id}", lambda z=zone: print(
                    f"  zone [{z.id}] {z.name}: level={z.level} on={z.is_on}"))
            for area in hub.areas.values():
                hub.subscribe(f"area:{area.id}", lambda a=area: print(
                    f"  area [{a.id}] {a.name}: occupancy={a.occupancy} power={a.power}"))
            await asyncio.Event().wait()
    except LeapError as err:
        print(f"FAILED: {type(err).__name__}: {err}")
        return 1
    finally:
        await hub.async_stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
