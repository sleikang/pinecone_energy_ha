"""Number entities (AC current limit / DC power limit)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import PineconeError, _to_float
from .const import DOMAIN
from .coordinator import PineconeCoordinator
from .entity import PineconeEntity


def _ac_max(coordinator: PineconeCoordinator, code: str) -> int:
    detail = coordinator.details(code).get("detail") or {}
    relation = coordinator.details(code).get("relation") or {}
    # Real payload: guns[0].ratedCurrent == 32.0 for a 21 kW AC pile.
    guns = detail.get("guns")
    if isinstance(guns, list) and guns and isinstance(guns[0], dict):
        rated = _to_float(guns[0].get("ratedCurrent"))
        if rated and 6 <= rated <= 40:
            return int(round(rated))
    for source in (detail, relation):
        for key in ("initPower", "presentPower", "power", "pilePower"):
            value = _to_float(source.get(key))
            if value:
                if abs(value - 3.5) < 0.6 or abs(value - 11) < 0.6:
                    return 16
                if value >= 20:
                    return 32
    return 32


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
            [PineconeLimitNumber(coordinator, code, coordinator.pile_type(code)) for code in new_codes]
        )

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class PineconeLimitNumber(PineconeEntity, NumberEntity):
    """AC: current limit in A. DC: power limit in % of nominal power."""

    _attr_mode = NumberMode.SLIDER
    _attr_should_poll = False

    def __init__(self, coordinator: PineconeCoordinator, pile_code: str, pile_type: str) -> None:
        super().__init__(coordinator, pile_code)
        self.pile_type = pile_type
        if pile_type == "ac":
            self.entity_description = NumberEntityDescription(
                key="current_limit",
                name="电流上限",
                native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
                device_class=NumberDeviceClass.CURRENT,
                icon="mdi:current-ac",
            )
            self._attr_native_min_value = 6
            self._attr_native_max_value = _ac_max(coordinator, pile_code)
            self._attr_native_step = 1
        else:
            self.entity_description = NumberEntityDescription(
                key="power_limit",
                name="功率上限",
                native_unit_of_measurement=PERCENTAGE,
                icon="mdi:speedometer",
            )
            self._attr_native_min_value = 0
            self._attr_native_max_value = 100
            self._attr_native_step = 5
        self._attr_unique_id = f"{pile_code}_{self.entity_description.key}"
        self._attr_translation_key = self.entity_description.key

    @property
    def native_value(self) -> float | None:
        data = self.data
        if self.pile_type == "ac":
            raw = data.get("strategy") or {}
            node = raw.get("data", raw) if isinstance(raw, dict) else raw
            if isinstance(node, list):
                node = node[0] if node else {}
            if isinstance(node, dict):
                value = _to_float(node.get("parameter"))
                if value is not None:
                    return value
            detail = data.get("detail") or {}
            return _to_float(detail.get("initPower"))
        detail = data.get("detail") or {}
        value = _to_float(detail.get("presentPower"))
        if value is not None and 0 <= value <= 100:
            return value
        return None

    async def async_set_native_value(self, value: float) -> None:
        try:
            if self.pile_type == "ac":
                await self.coordinator.client.set_ac_current_limit(self.pile_code, int(value))
            else:
                await self.coordinator.client.set_dc_power_limit(self.pile_code, int(value))
        except PineconeError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
