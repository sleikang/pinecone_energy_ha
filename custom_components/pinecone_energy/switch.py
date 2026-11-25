"""Switches for 松果能源 charging piles."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import PineconeError
from .const import DOMAIN
from .coordinator import PineconeCoordinator
from .entity import PineconeEntity

_LOGGER = logging.getLogger(__name__)


def _dotted(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        node: Any = data
        for part in name.split("."):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if node is not None and node != "":
            return node
    return None


def _flag(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None
    if num == 255:
        return None
    return num == 1 or num == 170


async def _set_silent(coordinator: PineconeCoordinator, code: str, on: bool) -> None:
    await coordinator.client.set_silent_mode(code, on)


async def _set_recovery(coordinator: PineconeCoordinator, code: str, on: bool) -> None:
    await coordinator.client.set_self_recovery(code, on)


async def _set_plug(coordinator: PineconeCoordinator, code: str, on: bool) -> None:
    await coordinator.client.set_plug_and_charge(code, on)


async def _set_public(coordinator: PineconeCoordinator, code: str, on: bool) -> None:
    await coordinator.client.set_public_pile(code, on)


async def _set_plan(coordinator: PineconeCoordinator, code: str, on: bool) -> None:
    from .plan import async_apply_plan

    await async_apply_plan(coordinator, code, enabled=on)


async def _set_charging(coordinator: PineconeCoordinator, code: str, on: bool) -> None:
    if on:
        await coordinator.client.start_charge(code)
        return
    order = (coordinator.details(code).get("charging") or {}).get("order_code")
    if not order:
        raw = await coordinator.client.current_charging(code)
        order = (coordinator.client.normalise_charging(raw) or {}).get("order_code")
    if not order:
        raise PineconeError("未获取到充电订单号，无法停止充电")
    await coordinator.client.stop_charge(code, str(order))


SWITCHES: tuple[SwitchEntityDescription, ...] = (
    SwitchEntityDescription(
        key="charge",
        name="开始充电",
        icon="mdi:ev-plug-ccs2",
        device_class=SwitchDeviceClass.SWITCH,
    ),
    SwitchEntityDescription(
        key="silent_mode",
        name="静音模式",
        icon="mdi:volume-off",
        device_class=SwitchDeviceClass.SWITCH,
        entity_registry_enabled_default=True,
    ),
    SwitchEntityDescription(
        key="self_recovery",
        name="智联续充",
        icon="mdi:restart",
        device_class=SwitchDeviceClass.SWITCH,
    ),
    SwitchEntityDescription(
        key="plug_and_charge",
        name="即插即充",
        icon="mdi:ev-plug-type2",
        device_class=SwitchDeviceClass.SWITCH,
    ),
    SwitchEntityDescription(
        key="plan_enabled",
        name="计划充电",
        icon="mdi:calendar-clock",
        device_class=SwitchDeviceClass.SWITCH,
    ),
    SwitchEntityDescription(
        key="public_pile",
        name="公共充电桩",
        icon="mdi:share-variant",
        device_class=SwitchDeviceClass.SWITCH,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PineconeCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    def _add_new() -> None:
        new_codes = [code for code in coordinator.devices if code not in known]
        if not new_codes:
            return
        known.update(new_codes)
        async_add_entities(
            [
                PineconeSwitch(coordinator, code, description)
                for code in new_codes
                for description in SWITCHES
            ]
        )

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class PineconeSwitch(PineconeEntity, SwitchEntity):
    entity_description: SwitchEntityDescription

    def __init__(
        self,
        coordinator: PineconeCoordinator,
        pile_code: str,
        description: SwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator, pile_code)
        self.entity_description = description
        self._attr_unique_id = f"{pile_code}_{description.key}"
        self._attr_translation_key = description.key

    @property
    def is_on(self) -> bool | None:
        key = self.entity_description.key
        if key == "charge":
            return self.status_code == 2 or self.status_text == "充电中"
        detail = self._source()
        if key == "silent_mode":
            return _flag(_dotted(detail, "silentMode", "pile.silentMode"))
        if key == "self_recovery":
            return _flag(_dotted(detail, "selfRecoveryStatus", "pile.selfRecoveryStatus"))
        if key == "plug_and_charge":
            auth = _flag(_dotted(detail, "authStatus", "pile.authStatus"))
            return None if auth is None else not auth
        if key == "public_pile":
            relation = self.data.get("relation") or {}
            share = _flag(relation.get("shareStatus") if "shareStatus" in relation else detail.get("shareStatus"))
            return share
        if key == "plan_enabled":
            return bool((self.data.get("plan") or {}).get("enabled"))
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_apply(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_apply(False)

    async def _async_apply(self, on: bool) -> None:
        handlers: dict[str, Callable[[PineconeCoordinator, str, bool], Awaitable[None]]] = {
            "charge": _set_charging,
            "silent_mode": _set_silent,
            "self_recovery": _set_recovery,
            "plug_and_charge": _set_plug,
            "public_pile": _set_public,
            "plan_enabled": _set_plan,
        }
        handler = handlers[self.entity_description.key]
        try:
            await handler(self.coordinator, self.pile_code, on)
        except PineconeError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
