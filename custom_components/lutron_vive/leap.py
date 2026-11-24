"""Minimal asyncio LEAP client for Lutron Vive hubs.

LEAP is Lutron's JSON-over-TLS protocol (TCP 8081). Each message is one JSON
object terminated by CRLF:

    {"CommuniqueType": "...", "Header": {"Url": "...", "ClientTag": "..."},
     "Body": {...}}

This client implements the "Login with a username / password" integration
method of the Vive hub: connect with TLS, send an UpdateRequest to /login, then
read, subscribe and send commands.

It has no Home Assistant dependencies so it can be used and tested standalone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15.0
CONNECT_TIMEOUT = 15.0
# Hubs send the whole zone/area list in one line, so allow big lines.
READ_LIMIT = 8 * 1024 * 1024


class LeapError(Exception):
    """Base error for LEAP problems."""


class LeapConnectionError(LeapError):
    """The hub could not be reached or the connection dropped."""


class LeapAuthError(LeapError):
    """The hub rejected the username / password."""


class LeapLoginError(LeapError):
    """The hub refused the login request for a reason other than credentials."""


class LeapFingerprintError(LeapConnectionError):
    """The hub's TLS certificate does not match the one seen at setup."""


@dataclass
class LeapResponse:
    """A parsed LEAP message."""

    communique_type: str
    url: str
    status_code: int | None
    status_text: str
    body_type: str | None
    body: dict[str, Any]
    client_tag: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return True for 2xx responses."""
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def has_content(self) -> bool:
        """Return True for a 2xx response that carries a body."""
        return self.ok and self.status_code != 204 and bool(self.body)

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> LeapResponse:
        """Build a response from a decoded JSON message."""
        header = message.get("Header") or {}
        status = str(header.get("StatusCode") or "")
        code: int | None = None
        text = status
        if status:
            first, _, rest = status.partition(" ")
            try:
                code = int(first)
                text = rest
            except ValueError:
                code = None
        body = message.get("Body")
        return cls(
            communique_type=str(message.get("CommuniqueType") or ""),
            url=str(header.get("Url") or ""),
            status_code=code,
            status_text=text,
            body_type=header.get("MessageBodyType"),
            body=body if isinstance(body, dict) else {},
            client_tag=header.get("ClientTag"),
            raw=message,
        )


SubscriptionCallback = Callable[[LeapResponse], None]

# LEAP client major versions to announce, tried in order.
CLIENT_MAJOR_VERSIONS: tuple[int, ...] = (1, 3, 2)


def _login_bodies(username: str, password: str) -> list[tuple[str, dict[str, Any]]]:
    """Login bodies to try, most likely first."""
    variants: list[tuple[str, dict[str, Any]]] = []
    for context in ("Application", None):
        for key in ("LoginID", "LoginId"):
            login: dict[str, Any] = {key: username, "Password": password}
            if context:
                login = {"ContextType": context, **login}
            label = f"{key}/{context or 'no-context'}"
            variants.append((label, {"Login": login}))
    return variants


def _response_detail(response: LeapResponse) -> str:
    """Short human-readable form of an error reply."""
    message = response.body.get("Message") if response.body else None
    status = f"{response.status_code} {response.status_text}".strip()
    if not response.status_code:
        status = response.communique_type or "no status"
    return f"{status} - {message}" if message else status


def build_ssl_context() -> ssl.SSLContext:
    """Create the TLS context used to talk to a hub.

    Vive hubs present a certificate issued by a private Lutron CA for a
    hostname that never matches the LAN address, so normal chain / hostname
    verification cannot succeed. Instead the certificate fingerprint is pinned
    at setup time and compared on every reconnect (see LeapClient).

    This does blocking work; call it from an executor inside Home Assistant.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class LeapClient:
    """One LEAP connection to a hub."""

    def __init__(
        self,
        host: str,
        port: int = 8081,
        *,
        ssl_context: ssl.SSLContext | None = None,
        fingerprint: str | None = None,
    ) -> None:
        """Initialize the client.

        fingerprint is the expected SHA-256 hex digest of the hub's TLS
        certificate. When given, connecting to a hub with any other
        certificate fails.
        """
        self.host = host
        self.port = port
        self._ssl_context = ssl_context
        self._expected_fingerprint = fingerprint.lower() if fingerprint else None
        self.fingerprint: str | None = None
        self.login_variant: str | None = None
        self.client_version: str | None = None

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._tag = 0
        self._pending: dict[str, asyncio.Future[LeapResponse]] = {}
        self._subscriptions: dict[str, SubscriptionCallback] = {}
        self._unsolicited: list[SubscriptionCallback] = []
        self._closed = asyncio.Event()
        self._closed.set()
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        """Return True while the connection is up."""
        return not self._closed.is_set()

    async def wait_closed(self) -> None:
        """Block until the connection has dropped or been closed."""
        await self._closed.wait()

    def add_unsolicited_callback(self, callback: SubscriptionCallback) -> None:
        """Receive messages that match no request and no subscription."""
        self._unsolicited.append(callback)

    # ------------------------------------------------------------- connection

    async def connect(self) -> None:
        """Open the TLS connection and start reading."""
        if self.connected:
            return
        context = self._ssl_context or await asyncio.get_running_loop().run_in_executor(
            None, build_ssl_context
        )
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                reader, writer = await asyncio.open_connection(
                    self.host, self.port, ssl=context, limit=READ_LIMIT
                )
        except (OSError, TimeoutError, ssl.SSLError) as err:
            raise LeapConnectionError(
                f"Could not connect to {self.host}:{self.port}: {err!r}"
            ) from err

        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        self.fingerprint = hashlib.sha256(der).hexdigest() if der else None
        if self._expected_fingerprint and self.fingerprint != self._expected_fingerprint:
            writer.close()
            raise LeapFingerprintError(
                f"TLS certificate of {self.host} changed "
                f"(expected {self._expected_fingerprint}, got {self.fingerprint})"
            )

        self._reader = reader
        self._writer = writer
        self._pending.clear()
        self._subscriptions.clear()
        self._closed.clear()
        self._read_task = asyncio.create_task(
            self._read_loop(), name=f"lutron_vive read {self.host}"
        )

    async def close(self) -> None:
        """Close the connection."""
        task = self._read_task
        self._read_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._teardown(LeapConnectionError("Connection closed"))

    def _teardown(self, error: Exception) -> None:
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        self._subscriptions.clear()
        self._closed.set()

    async def _read_loop(self) -> None:
        reader = self._reader
        assert reader is not None
        error: Exception = LeapConnectionError("Connection closed by hub")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    _LOGGER.debug("Ignoring non-JSON line from hub: %r", line[:200])
                    continue
                if isinstance(message, dict):
                    self._dispatch(LeapResponse.from_message(message))
        except asyncio.CancelledError:
            raise
        except (OSError, ssl.SSLError, ValueError, asyncio.IncompleteReadError) as err:
            error = LeapConnectionError(f"Connection lost: {err!r}")
        finally:
            self._teardown(error)

    def _dispatch(self, response: LeapResponse) -> None:
        _LOGGER.debug("<- %s", response.raw)
        tag = response.client_tag
        if tag is not None:
            future = self._pending.get(tag)
            if future is not None and not future.done():
                # "102 Processing" style interim replies are not the answer.
                if response.status_code is not None and response.status_code < 200:
                    return
                del self._pending[tag]
                future.set_result(response)
                return
            callback = self._subscriptions.get(tag)
            if callback is not None:
                self._safe_call(callback, response)
                return
        for callback in self._unsolicited:
            self._safe_call(callback, response)

    @staticmethod
    def _safe_call(callback: SubscriptionCallback, response: LeapResponse) -> None:
        try:
            callback(response)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error handling LEAP message for %s", response.url)

    # ---------------------------------------------------------------- requests

    def _next_tag(self) -> str:
        self._tag += 1
        return f"ha-{self._tag}"

    async def request(
        self,
        communique_type: str,
        url: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = REQUEST_TIMEOUT,
        _tag: str | None = None,
    ) -> LeapResponse:
        """Send a request and wait for its response."""
        writer = self._writer
        if writer is None or not self.connected:
            raise LeapConnectionError("Not connected")

        tag = _tag or self._next_tag()
        message: dict[str, Any] = {
            "CommuniqueType": communique_type,
            "Header": {"ClientTag": tag, "Url": url},
        }
        if body is not None:
            message["Body"] = body

        future: asyncio.Future[LeapResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[tag] = future

        if url == "/login":
            _LOGGER.debug("-> %s %s (credentials hidden)", communique_type, url)
        else:
            _LOGGER.debug("-> %s", message)
        try:
            async with self._write_lock:
                writer.write(json.dumps(message).encode() + b"\r\n")
                await writer.drain()
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as err:
            raise LeapConnectionError(
                f"Timed out waiting for {communique_type} {url}"
            ) from err
        except OSError as err:
            raise LeapConnectionError(f"Write failed: {err!r}") from err
        finally:
            self._pending.pop(tag, None)

    async def read(self, url: str) -> LeapResponse:
        """Send a ReadRequest."""
        return await self.request("ReadRequest", url)

    async def login(self, username: str, password: str) -> LeapResponse:
        """Authenticate with the hub's API integration username / password.

        The exact login body a Vive hub expects is not publicly documented, so
        a few plausible variants are tried in order. A hub answers a malformed
        body and a wrong password with similar errors, so the hub's own reply
        to every attempt is kept in the exception for troubleshooting.
        """
        attempts: list[str] = []
        auth_refusals = 0
        # Vive hubs insist the session starts with the client announcing its
        # LEAP version ("Your first request must be update request on
        # /clientsetting").
        setting_detail = await self.send_client_setting()
        if setting_detail:
            attempts.append(f"clientsetting: {setting_detail}")
        for label, body in _login_bodies(username, password):
            response = await self.request("UpdateRequest", "/login", body)
            if response.ok:
                _LOGGER.debug("Login accepted using variant %s", label)
                self.login_variant = label
                return response
            detail = _response_detail(response)
            attempts.append(f"{label}: {detail}")
            _LOGGER.debug("Login variant %s refused: %s", label, detail)
            if response.status_code in (401, 403):
                auth_refusals += 1
        summary = "; ".join(attempts)
        # A 401/403 means the hub understood that format and refused the
        # credentials; 400s on the other formats are just the wrong shape.
        if auth_refusals:
            raise LeapAuthError(f"Hub refused the username / password ({summary})")
        raise LeapLoginError(f"Hub did not accept the login request ({summary})")

    async def send_client_setting(self) -> str | None:
        """Announce the client's LEAP major version.

        Returns None on success, else the hub's reply to each attempt. Vive
        hubs refuse a ClientMinorVersion ("ClientMinorVersion is not
        modifiable"), so only the major version is sent.
        """
        details: list[str] = []
        for major in CLIENT_MAJOR_VERSIONS:
            response = await self.request(
                "UpdateRequest",
                "/clientsetting",
                {"ClientSetting": {"ClientMajorVersion": major}},
            )
            if response.ok:
                _LOGGER.debug("Client setting accepted as major version %s", major)
                self.client_version = str(major)
                return None
            detail = _response_detail(response)
            details.append(f"v{major}: {detail}")
            _LOGGER.debug("Client setting v%s refused: %s", major, detail)
        return ", ".join(details)

    async def subscribe(
        self, url: str, callback: SubscriptionCallback
    ) -> LeapResponse:
        """Subscribe to a resource.

        The hub answers once with the full current state, then pushes changes
        re-using the ClientTag of the subscribe request. The callback receives
        those pushes; the initial snapshot is returned.
        """
        tag = self._next_tag()
        self._subscriptions[tag] = callback
        try:
            response = await self.request("SubscribeRequest", url, _tag=tag)
        except LeapError:
            self._subscriptions.pop(tag, None)
            raise
        if not response.ok:
            self._subscriptions.pop(tag, None)
        return response

    async def command(self, url: str, command: dict[str, Any]) -> LeapResponse:
        """Send a command to a command processor."""
        return await self.request("CreateRequest", url, {"Command": command})
