"""Time entities for scheduled charging start / end."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import PineconeError
from .const import DOMAIN
from .coordinator import PineconeCoordinator
from .entity import PineconeEntity
from .plan import async_apply_plan, current_plan

KEYS = ("plan_start", "plan_end")


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
            [PineconePlanTime(coordinator, code, key) for code in new_codes for key in KEYS]
        )

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


def _parse(value: str) -> time | None:
    try:
        hour, minute = value.split(":")[:2]
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


class PineconePlanTime(PineconeEntity, TimeEntity):
    def __init__(self, coordinator: PineconeCoordinator, pile_code: str, key: str) -> None:
        super().__init__(coordinator, pile_code)
        self._key = key
        self._attr_unique_id = f"{pile_code}_{key}"
        self._attr_translation_key = key
        self._attr_icon = "mdi:clock-start" if key == "plan_start" else "mdi:clock-end"

    @property
    def native_value(self) -> time | None:
        plan = current_plan(self.coordinator, self.pile_code)
        return _parse(plan["start"] if self._key == "plan_start" else plan["end"])

    async def async_set_value(self, value: time) -> None:
        hhmm = f"{value.hour:02d}:{value.minute:02d}"
        try:
            if self._key == "plan_start":
                await async_apply_plan(self.coordinator, self.pile_code, start=hhmm)
            else:
                await async_apply_plan(self.coordinator, self.pile_code, end=hhmm)
        except (PineconeError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err
