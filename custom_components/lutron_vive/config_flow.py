"""Config flow for Lutron Vive."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import CONF_FINGERPRINT, DEFAULT_PORT, DOMAIN
from .hub import ViveHubInfo, async_probe
from .leap import (
    LeapAuthError,
    LeapError,
    LeapFingerprintError,
    LeapLoginError,
    build_ssl_context,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

DISCOVERY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class ViveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Lutron Vive."""

    VERSION = 1

    _discovered_host: str | None = None
    _discovered_name: str | None = None

    async def _probe(
        self, data: Mapping[str, Any], fingerprint: str | None = None
    ) -> tuple[ViveHubInfo | None, str | None, str | None]:
        """Try to log in. Returns (info, fingerprint, error key).

        The hub's reply, when there is one, is left in self._detail for the
        error message.
        """
        self._detail = ""
        ssl_context = await self.hass.async_add_executor_job(build_ssl_context)
        try:
            info, seen = await async_probe(
                data[CONF_HOST].strip(),
                data.get(CONF_PORT, DEFAULT_PORT),
                data[CONF_USERNAME].strip(),
                data[CONF_PASSWORD],
                fingerprint=fingerprint,
                ssl_context=ssl_context,
            )
        except LeapAuthError as err:
            _LOGGER.warning("Vive hub login refused: %s", err)
            self._detail = str(err)
            return None, None, "invalid_auth"
        except LeapLoginError as err:
            _LOGGER.warning("Vive hub login failed: %s", err)
            self._detail = str(err)
            return None, None, "login_failed"
        except LeapFingerprintError:
            return None, None, "fingerprint_changed"
        except LeapError as err:
            _LOGGER.warning("Could not connect to Vive hub: %s", err)
            self._detail = str(err)
            return None, None, "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error connecting to Vive hub")
            return None, None, "unknown"
        return info, seen, None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the hub address and API credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = {
                **user_input,
                CONF_HOST: user_input[CONF_HOST].strip(),
                CONF_USERNAME: user_input[CONF_USERNAME].strip(),
            }
            info, fingerprint, error = await self._probe(user_input)
            if error:
                errors["base"] = error
            else:
                assert info is not None
                return await self._create(user_input, info, fingerprint)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, user_input or {}
            ),
            errors=errors,
            description_placeholders={"detail": getattr(self, "_detail", "")},
        )

    async def _create(
        self,
        data: dict[str, Any],
        info: ViveHubInfo,
        fingerprint: str | None,
    ) -> ConfigFlowResult:
        """Create the entry once login worked."""
        unique_id = info.serial or fingerprint or data[CONF_HOST]
        await self.async_set_unique_id(str(unique_id), raise_on_progress=False)
        self._abort_if_unique_id_configured(updates={CONF_HOST: data[CONF_HOST]})
        title = info.name or info.project_name or "Lutron Vive"
        return self.async_create_entry(
            title=f"{title} ({data[CONF_HOST]})",
            data={**data, CONF_FINGERPRINT: fingerprint},
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a Vive hub found on the network."""
        addresses = [
            str(address)
            for address in (discovery_info.ip_addresses or [discovery_info.ip_address])
            if address is not None and address.version == 4
        ]
        if not addresses:
            return self.async_abort(reason="not_ipv4")
        host = addresses[0]
        properties = {
            str(key).lower(): value for key, value in discovery_info.properties.items()
        }
        serial = properties.get("sn")

        # Same hub, new address: update the stored address and stop.
        if serial:
            await self.async_set_unique_id(str(serial))
            self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        else:
            await self.async_set_unique_id(f"host_{host}")
        self._async_abort_entries_match({CONF_HOST: host})
        # The hub advertises more than one service; handle it only once.
        for flow in self._async_in_progress(include_uninitialized=True):
            if flow["flow_id"] != self.flow_id and flow["context"].get("host") == host:
                return self.async_abort(reason="already_in_progress")
        self.context["host"] = host

        name = properties.get("name") or discovery_info.name.split(".")[0]
        self._discovered_host = host
        self._discovered_name = str(name)
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the API credentials of a discovered hub."""
        assert self._discovered_host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_HOST: self._discovered_host,
                CONF_PORT: DEFAULT_PORT,
                CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            info, fingerprint, error = await self._probe(data)
            if error:
                errors["base"] = error
            else:
                assert info is not None
                return await self._create(data, info, fingerprint)
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=self.add_suggested_values_to_schema(
                DISCOVERY_SCHEMA, user_input or {}
            ),
            description_placeholders={
                "name": self._discovered_name or "",
                "host": self._discovered_host,
                "detail": getattr(self, "_detail", ""),
            },
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                **entry.data,
                **user_input,
                CONF_USERNAME: user_input[CONF_USERNAME].strip(),
            }
            _, _, error = await self._probe(data, entry.data.get(CONF_FINGERPRINT))
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                REAUTH_SCHEMA, {CONF_USERNAME: entry.data.get(CONF_USERNAME)}
            ),
            description_placeholders={
                "host": entry.data[CONF_HOST],
                "detail": getattr(self, "_detail", ""),
            },
            errors=errors,
        )
