#!/usr/bin/env python3
"""A tiny fake Vive hub that speaks LEAP over TLS, for testing without hardware.

    python3 tools/mock_hub.py --port 8081 --username ha --password secret

It is a simulation built from public protocol notes, not a copy of real hub
behaviour. Requires the `cryptography` package for the self-signed certificate.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import ssl
import tempfile
from pathlib import Path


def make_cert(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vive-mock-hub")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "hub.crt"
    key_path = directory / "hub.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class MockHub:
    """State and protocol handling of the fake hub."""

    def __init__(
        self,
        username: str = "ha",
        password: str = "secret",
        *,
        flat_zone_list: bool = True,
        collection_subscriptions: bool = True,
        login_key: str = "LoginID",
        login_context: str | None = "Application",
        require_client_setting: bool = True,
        accepted_major_versions: tuple[int, ...] = (1, 2, 3),
    ) -> None:
        self.require_client_setting = require_client_setting
        self.accepted_major_versions = accepted_major_versions
        # The only login body format this fake hub accepts; any other format
        # gets 400 BadRequest, like a real LEAP server.
        self.login_key = login_key
        self.login_context = login_context
        self.username = username
        self.password = password
        self.flat_zone_list = flat_zone_list
        self.collection_subscriptions = collection_subscriptions
        self.areas = {
            "1": {"Name": "Shop", "IsLeaf": False, "Parent": None},
            "2": {"Name": "Main Bay", "IsLeaf": True, "Parent": "1"},
            "3": {"Name": "Office", "IsLeaf": True, "Parent": "1"},
        }
        self.zones = {
            "10": {"Name": "Overheads", "ControlType": "Dimmed", "Area": "2"},
            "11": {"Name": "Bench Lights", "ControlType": "Dimmed", "Area": "2"},
            "12": {"Name": "Exhaust Fan", "ControlType": "Switched", "Area": "2"},
            "13": {"Name": "Outlets", "ControlType": "Receptacle", "Area": "3"},
            "14": {"Name": "Door Relay", "ControlType": "CCO", "Area": "3"},
        }
        self.zone_state: dict[str, dict] = {
            "10": {"Level": 75},
            "11": {"Level": 0},
            "12": {"Level": 0, "SwitchedLevel": "Off"},
            "13": {"Level": 100, "ReceptacleLevel": "On"},
            "14": {"Level": 0, "CCOLevel": "Open"},
        }
        self.area_state: dict[str, dict] = {
            "2": {"OccupancyStatus": "Occupied", "InstantaneousPower": 840},
            "3": {"OccupancyStatus": "Unoccupied", "InstantaneousPower": 12},
        }
        self.commands: list[tuple[str, dict]] = []
        # Pico remotes. button_mode: "collection" (/buttongroup + /button),
        # "expanded" (/device/{id}/buttongroup/expanded only) or "none".
        self.button_mode = "collection"
        self.button_events_supported = True
        self.devices = {
            "20": {"Name": "Pico", "DeviceType": "Pico3ButtonRaiseLower",
                   "ModelNumber": "PJ2-3BRL-GWH-L01", "SerialNumber": 55501, "Area": "2"},
            "21": {"Name": "Sensor", "DeviceType": "RPSOccupancySensor",
                   "ModelNumber": "LRF2-OCR2B-P-WH", "SerialNumber": 55502, "Area": "3"},
        }
        self.button_groups = {"50": {"Device": "20", "Buttons": ["101", "102", "103", "104", "105"]}}
        self.button_defs = {
            "101": {"ButtonNumber": 2, "Name": "Button 2", "Engraving": {"Text": "On"}},
            "102": {"ButtonNumber": 3, "Name": "Button 3"},
            "103": {"ButtonNumber": 4, "Name": "Button 4", "Engraving": {"Text": "Favorite"}},
            "104": {"ButtonNumber": 5, "Name": "Button 5"},
            "105": {"ButtonNumber": 6, "Name": "Button 6", "Engraving": {"Text": "Off"}},
        }
        self._clients: list[_Client] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self._tmp: tempfile.TemporaryDirectory | None = None

    # -- bodies ---------------------------------------------------------------

    def zone_def(self, zone_id: str) -> dict:
        zone = self.zones[zone_id]
        return {
            "href": f"/zone/{zone_id}",
            "Name": zone["Name"],
            "ControlType": zone["ControlType"],
            "AvailableControlTypes": [zone["ControlType"]],
            "AssociatedArea": {"href": f"/area/{zone['Area']}"},
        }

    def zone_status(self, zone_id: str) -> dict:
        return {
            "href": f"/zone/{zone_id}/status",
            "Zone": {"href": f"/zone/{zone_id}"},
            "StatusAccuracy": "Good",
            **self.zone_state[zone_id],
        }

    def area_status(self, area_id: str) -> dict:
        return {"href": f"/area/{area_id}/status", **self.area_state.get(area_id, {})}

    # -- server ---------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        cert, key = make_cert(Path(self._tmp.name))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self._server = await asyncio.start_server(
            self._handle, host, port, ssl=context
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        self.drop_clients()
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def drop_clients(self) -> None:
        for client in list(self._clients):
            client.writer.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client = _Client(self, writer)
        self._clients.append(client)
        try:
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                client.handle(message)
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            self._clients.remove(client)
            writer.close()

    # -- state changes that push to subscribers -------------------------------

    def set_zone(self, zone_id: str, **changes) -> None:
        self.zone_state[zone_id].update(changes)
        status = self.zone_status(zone_id)
        for client in self._clients:
            client.push_zone(zone_id, status)

    def press(self, button_id: str, event_type: str = "Press") -> None:
        for client in self._clients:
            client.push_button(button_id, event_type)

    def button_def(self, button_id: str) -> dict:
        group = next(g for g, d in self.button_groups.items() if button_id in d["Buttons"])
        return {"href": f"/button/{button_id}", "Parent": {"href": f"/buttongroup/{group}"},
                **self.button_defs[button_id]}

    def set_area(self, area_id: str, **changes) -> None:
        self.area_state.setdefault(area_id, {}).update(changes)
        delta = {"href": f"/area/{area_id}/status", **changes}
        for client in self._clients:
            client.push_area(area_id, delta)


class _Client:
    def __init__(self, hub: MockHub, writer: asyncio.StreamWriter) -> None:
        self.hub = hub
        self.writer = writer
        self.logged_in = False
        self.client_set = False
        self.zone_tag: str | None = None
        self.area_tag: str | None = None
        self.area_tags: dict[str, str] = {}
        self.button_tags: dict[str, str] = {}

    def send(self, ctype, url, status, body=None, body_type=None, tag=None) -> None:
        header = {"StatusCode": status, "Url": url}
        if body_type:
            header["MessageBodyType"] = body_type
        if tag is not None:
            header["ClientTag"] = tag
        message = {"CommuniqueType": ctype, "Header": header}
        if body is not None:
            message["Body"] = body
        self.writer.write(json.dumps(message).encode() + b"\r\n")

    def push_zone(self, zone_id: str, status: dict) -> None:
        if self.zone_tag:
            self.send("ReadResponse", "/zone/status", "200 OK",
                      {"ZoneStatuses": [status]}, "MultipleZoneStatus", self.zone_tag)

    def push_button(self, button_id: str, event_type: str) -> None:
        tag = self.button_tags.get(button_id)
        if tag:
            self.send("ReadResponse", f"/button/{button_id}/status/event", "200 OK",
                      {"ButtonStatus": {"Button": {"href": f"/button/{button_id}"},
                                        "ButtonEvent": {"EventType": event_type}}},
                      "OneButtonStatusEvent", tag)

    def push_area(self, area_id: str, delta: dict) -> None:
        if self.area_tag:
            self.send("ReadResponse", "/area/status", "200 OK",
                      {"AreaStatuses": [delta]}, "MultipleAreaStatus", self.area_tag)
        elif area_id in self.area_tags:
            self.send("ReadResponse", f"/area/{area_id}/status", "200 OK",
                      {"AreaStatus": delta}, "OneAreaStatus", self.area_tags[area_id])

    def handle(self, message: dict) -> None:
        hub = self.hub
        ctype = message.get("CommuniqueType", "")
        header = message.get("Header", {})
        url = header.get("Url", "")
        tag = header.get("ClientTag")
        body = message.get("Body") or {}
        reply = ctype.replace("Request", "Response")

        def error(status: str, text: str) -> None:
            self.send("ExceptionResponse", url, status,
                      {"Message": text}, "ExceptionDetail", tag)

        if hub.require_client_setting and not self.client_set:
            if url == "/clientsetting" and ctype == "UpdateRequest":
                setting = body.get("ClientSetting", {})
                if "ClientMinorVersion" in setting:
                    error("400 BadRequest", "ClientMinorVersion is not modifiable")
                    return
                if isinstance(setting.get("ClientMajorVersion"), int) and \
                        setting["ClientMajorVersion"] in hub.accepted_major_versions:
                    self.client_set = True
                    self.send(reply, url, "200 OK", {"ClientSetting": {
                        "href": "/clientsetting", **setting}}, "OneClientSettingDefinition", tag)
                else:
                    error("400 BadRequest", "Unsupported client version")
                return
            error("400 BadRequest",
                  "Your first request must be update request on /clientsetting")
            return

        if url == "/login" and ctype == "UpdateRequest":
            login = body.get("Login", {})
            if hub.login_key not in login or login.get("ContextType") != hub.login_context:
                error("400 BadRequest", "This request is not supported")
                return
            user = login[hub.login_key]
            if user == hub.username and login.get("Password") == hub.password:
                self.logged_in = True
                self.send(reply, url, "200 OK",
                          {"Login": {"href": "/login", "LoginID": user}},
                          "OneLoginDefinition", tag)
            else:
                error("401 Unauthorized", "Invalid credentials")
            return
        if not self.logged_in:
            error("401 Unauthorized", "Login required")
            return

        parts = url.strip("/").split("/")
        if ctype == "ReadRequest":
            if url.startswith("/server") and url.endswith("/ping"):
                self.send(reply, url, "200 OK",
                          {"PingResponse": {"LEAPVersion": 1.115}}, "OnePingResponse", tag)
            elif url == "/device?where=IsThisDevice:true":
                self.send(reply, url, "200 OK", {"Devices": [{
                    "href": "/device/1", "Name": "Vive Hub", "DeviceType": "ViveHub",
                    "ModelNumber": "HJS-2-FM", "SerialNumber": 12345678,
                    "FirmwareImage": {"Firmware": {"DisplayName": "01.14.05"}},
                }]}, "MultipleDeviceDefinition", tag)
            elif url == "/device":
                devices = [{"href": "/device/1", "Name": "Vive Hub", "DeviceType": "ViveHub",
                            "IsThisDevice": True}] + [{
                    "href": f"/device/{i}", "Name": d["Name"], "DeviceType": d["DeviceType"],
                    "ModelNumber": d["ModelNumber"], "SerialNumber": d["SerialNumber"],
                    "AssociatedArea": {"href": f"/area/{d['Area']}"},
                } for i, d in hub.devices.items()]
                self.send(reply, url, "200 OK", {"Devices": devices},
                          "MultipleDeviceDefinition", tag)
            elif url == "/buttongroup" and hub.button_mode == "collection":
                self.send(reply, url, "200 OK", {"ButtonGroups": [{
                    "href": f"/buttongroup/{g}", "Parent": {"href": f"/device/{d['Device']}"},
                    "Buttons": [{"href": f"/button/{b}"} for b in d["Buttons"]],
                } for g, d in hub.button_groups.items()]}, "MultipleButtonGroupDefinition", tag)
            elif url == "/button" and hub.button_mode == "collection":
                self.send(reply, url, "200 OK",
                          {"Buttons": [hub.button_def(b) for b in hub.button_defs]},
                          "MultipleButtonDefinition", tag)
            elif (len(parts) == 4 and parts[0] == "device" and parts[2:] == ["buttongroup", "expanded"]
                  and hub.button_mode == "expanded"):
                groups = [{"href": f"/buttongroup/{g}",
                           "Buttons": [hub.button_def(b) for b in d["Buttons"]]}
                          for g, d in hub.button_groups.items() if d["Device"] == parts[1]]
                if groups:
                    self.send(reply, url, "200 OK", {"ButtonGroupsExpanded": groups},
                              "MultipleButtonGroupExpandedDefinition", tag)
                else:
                    self.send(reply, url, "204 NoContent", None, None, tag)
            elif url == "/project":
                self.send(reply, url, "200 OK",
                          {"Project": {"href": "/project", "Name": "Mock Shop",
                                       "ProductType": "Lutron Vive Project"}},
                          "OneProjectDefinition", tag)
            elif url == "/area":
                areas = [{
                    "href": f"/area/{i}", "Name": a["Name"], "IsLeaf": a["IsLeaf"],
                    **({"Parent": {"href": f"/area/{a['Parent']}"}} if a["Parent"] else {}),
                } for i, a in hub.areas.items()]
                self.send(reply, url, "200 OK", {"Areas": areas},
                          "MultipleAreaDefinition", tag)
            elif url == "/zone":
                if hub.flat_zone_list:
                    self.send(reply, url, "200 OK",
                              {"Zones": [hub.zone_def(z) for z in hub.zones]},
                              "MultipleZoneDefinition", tag)
                else:
                    error("405 MethodNotAllowed", "This request is not supported")
            elif len(parts) == 3 and parts[0] == "area" and parts[2] == "associatedzone":
                zones = [hub.zone_def(z) for z, d in hub.zones.items() if d["Area"] == parts[1]]
                if zones:
                    self.send(reply, url, "200 OK", {"Zones": zones},
                              "MultipleZoneDefinition", tag)
                else:
                    self.send(reply, url, "204 NoContent", None, None, tag)
            elif len(parts) == 3 and parts[0] == "zone" and parts[2] == "status" and parts[1] in hub.zones:
                self.send(reply, url, "200 OK", {"ZoneStatus": hub.zone_status(parts[1])},
                          "OneZoneStatus", tag)
            elif len(parts) == 3 and parts[0] == "area" and parts[2] == "status" and parts[1] in hub.areas:
                self.send(reply, url, "200 OK", {"AreaStatus": hub.area_status(parts[1])},
                          "OneAreaStatus", tag)
            else:
                error("400 BadRequest", "This request is not supported")
        elif ctype == "SubscribeRequest":
            if url == "/zone/status" and hub.collection_subscriptions:
                self.zone_tag = tag
                self.send(reply, url, "200 OK",
                          {"ZoneStatuses": [hub.zone_status(z) for z in hub.zones]},
                          "MultipleZoneStatus", tag)
            elif url == "/area/status" and hub.collection_subscriptions:
                self.area_tag = tag
                self.send(reply, url, "200 OK",
                          {"AreaStatuses": [hub.area_status(a) for a in hub.areas]},
                          "MultipleAreaStatus", tag)
            elif (len(parts) == 4 and parts[0] == "button" and parts[2:] == ["status", "event"]
                  and parts[1] in hub.button_defs and hub.button_events_supported):
                self.button_tags[parts[1]] = tag
                self.send(reply, url, "200 OK", {"ButtonStatus": {
                    "Button": {"href": f"/button/{parts[1]}"},
                    "ButtonEvent": {"EventType": "Release"}}}, "OneButtonStatusEvent", tag)
            elif len(parts) == 3 and parts[0] == "area" and parts[2] == "status" and parts[1] in hub.areas:
                self.area_tags[parts[1]] = tag
                self.send(reply, url, "200 OK", {"AreaStatus": hub.area_status(parts[1])},
                          "OneAreaStatus", tag)
            else:
                error("405 MethodNotAllowed", "This request is not supported")
        elif ctype == "CreateRequest" and len(parts) == 3 and parts[0] == "zone" \
                and parts[2] == "commandprocessor" and parts[1] in hub.zones:
            zone_id = parts[1]
            command = body.get("Command", {})
            kind = hub.zones[zone_id]["ControlType"]
            name = command.get("CommandType")
            hub.commands.append((zone_id, command))
            changes = None
            if name == "GoToDimmedLevel" and kind == "Dimmed":
                changes = {"Level": command["DimmedLevelParameters"]["Level"]}
            elif name == "GoToSwitchedLevel" and kind == "Switched":
                state = command["SwitchedLevelParameters"]["SwitchedLevel"]
                changes = {"SwitchedLevel": state, "Level": 100 if state == "On" else 0}
            elif name == "GoToReceptacleLevel" and kind == "Receptacle":
                state = command["ReceptacleLevelParameters"]["ReceptacleLevel"]
                changes = {"ReceptacleLevel": state, "Level": 100 if state == "On" else 0}
            elif name == "GoToCCOLevel" and kind == "CCO":
                state = command["CCOLevelParameters"]["CCOLevel"]
                changes = {"CCOLevel": state, "Level": 100 if state == "Closed" else 0}
            if name in ("Raise", "Lower", "Stop") and kind == "Dimmed":
                self.send(reply, url, "201 Created", None, None, tag)
                return
            if changes is None:
                error("400 BadRequest", "Command not valid for this zone")
                return
            self.send(reply, url, "201 Created", None, None, tag)
            hub.set_zone(zone_id, **changes)
        else:
            error("400 BadRequest", "This request is not supported")


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--username", default="ha")
    parser.add_argument("--password", default="secret")
    args = parser.parse_args()
    hub = MockHub(args.username, args.password)
    await hub.start(args.host, args.port)
    print(f"Mock Vive hub listening on {args.host}:{hub.port} "
          f"(login {args.username} / {args.password}). Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await hub.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
