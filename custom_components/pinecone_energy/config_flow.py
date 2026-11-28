"""Config flow for 松果能源."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .api import PineconeAuthError, PineconeClient, PineconeError
from .const import (
    CONF_HISTORY_DAYS,
    CONF_PHONE,
    CONF_PILES,
    CONF_TOKEN,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_HISTORY_DAYS,
    MAX_SCAN_INTERVAL,
    MIN_HISTORY_DAYS,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
CONF_SCAN_INTERVAL = "scan_interval"
CONF_CODE = "code"

PHONE_RE = re.compile(r"^1\d{10}$")


class PineconeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two step SMS login flow, then pick the piles."""

    VERSION = 1

    def __init__(self) -> None:
        self._phone: str = ""
        self._token: str = ""
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = str(user_input[CONF_PHONE]).strip()
            if not PHONE_RE.match(phone):
                errors["base"] = "invalid_phone"
            else:
                client = PineconeClient(async_get_clientsession(self.hass))
                try:
                    await client.send_code(phone)
                except PineconeError as err:
                    _LOGGER.warning("发送验证码失败: %s", err)
                    errors["base"] = "send_code_failed"
                else:
                    self._phone = phone
                    return await self.async_step_code()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_PHONE): TextSelector()}),
            errors=errors,
            description_placeholders={"hint": "输入松果能源 APP 登录手机号，我们会发送短信验证码"},
        )

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = str(user_input[CONF_CODE]).strip()
            client = PineconeClient(async_get_clientsession(self.hass))
            try:
                await client.login_with_code(self._phone, code)
                self._devices = await client.list_devices()
            except PineconeAuthError as err:
                errors["base"] = "invalid_auth"
                _LOGGER.debug("登录失败: %s", err)
            except PineconeError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("登录失败: %s", err)
            else:
                if not self._devices:
                    errors["base"] = "no_devices"
                else:
                    self._token = client.token or ""
                    return await self.async_step_piles()

        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required(CONF_CODE): TextSelector()}),
            errors=errors,
            description_placeholders={"phone": self._phone},
        )

    def _pile_options(self) -> dict[str, str]:
        options: dict[str, str] = {}
        for item in self._devices:
            code = str(item.get("pileCode") or "").strip()
            if not code:
                continue
            name = str(item.get("pileName") or item.get("sharePileName") or code)
            options[code] = f"{name} ({code})"
        return options

    async def async_step_piles(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        options = self._pile_options()
        if user_input is not None:
            selected = [str(x) for x in user_input.get(CONF_PILES, [])]
            if not selected:
                errors["base"] = "no_devices"
            else:
                return self.async_create_entry(
                    title="松果能源",
                    data={
                        CONF_PHONE: self._phone,
                        CONF_TOKEN: self._token,
                        CONF_PILES: selected,
                    },
                    options={
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                        CONF_HISTORY_DAYS: DEFAULT_HISTORY_DAYS,
                    },
                )

        return self.async_show_form(
            step_id="piles",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PILES, default=list(options)): SelectSelector(
                        SelectSelectorConfig(
                            options=[{"value": k, "label": v} for k, v in options.items()],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------- reauth
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        self._phone = str(entry_data.get(CONF_PHONE) or "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = PineconeClient(async_get_clientsession(self.hass))
            try:
                await client.send_code(self._phone)
            except PineconeError:
                errors["base"] = "send_code_failed"
            else:
                return await self.async_step_reauth_code()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"phone": self._phone},
        )

    async def async_step_reauth_code(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = PineconeClient(async_get_clientsession(self.hass))
            try:
                await client.login_with_code(self._phone, str(user_input[CONF_CODE]).strip())
            except PineconeError:
                errors["base"] = "invalid_auth"
            else:
                entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
                if entry is None:
                    return self.async_abort(reason="unknown")
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, CONF_TOKEN: client.token, CONF_PHONE: self._phone},
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_code",
            data_schema=vol.Schema({vol.Required(CONF_CODE): TextSelector()}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PineconeOptionsFlow:
        return PineconeOptionsFlow(config_entry)


class PineconeOptionsFlow(OptionsFlow):
    """Change polling interval and the selected piles."""

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        client = PineconeClient(
            async_get_clientsession(self.hass),
            token=self.entry.data.get(CONF_TOKEN),
            phone=self.entry.data.get(CONF_PHONE),
        )
        try:
            devices = await client.list_devices()
        except PineconeError:
            devices = [
                {"pileCode": code, "pileName": code}
                for code in self.entry.data.get(CONF_PILES, [])
            ]

        options = {
            str(d.get("pileCode")): f"{d.get('pileName') or d.get('sharePileName') or d.get('pileCode')}"
            for d in devices
            if d.get("pileCode")
        }

        if user_input is not None:
            selected = [str(x) for x in user_input.get(CONF_PILES, [])]
            if not selected:
                errors["base"] = "no_devices"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_PILES: selected,
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_HISTORY_DAYS: int(
                            user_input.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
                        ),
                    },
                )

        current = list(self.entry.data.get(CONF_PILES) or [])
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PILES, default=current): SelectSelector(
                        SelectSelectorConfig(
                            options=[{"value": k, "label": v} for k, v in options.items()],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=int(self.entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=5,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_HISTORY_DAYS,
                        default=int(
                            self.entry.options.get(
                                CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS
                            )
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_HISTORY_DAYS,
                            max=MAX_HISTORY_DAYS,
                            step=1,
                            unit_of_measurement="天",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
            errors=errors,
        )
