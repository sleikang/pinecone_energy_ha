"""Select entity for the scheduled charging repeat mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import PineconeError
from .const import DOMAIN
from .coordinator import PineconeCoordinator
from .entity import PineconeEntity
from .plan import async_apply_plan, current_plan

OPTIONS = ["DAILY", "ONCE"]
LABELS = {"DAILY": "每天", "ONCE": "单次"}


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
        async_add_entities([PineconeRepeatSelect(coordinator, code) for code in new_codes])

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class PineconeRepeatSelect(PineconeEntity, SelectEntity):
    _attr_options = OPTIONS
    _attr_icon = "mdi:repeat"

    def __init__(self, coordinator: PineconeCoordinator, pile_code: str) -> None:
        super().__init__(coordinator, pile_code)
        self._attr_unique_id = f"{pile_code}_plan_repeat"
        self._attr_translation_key = "plan_repeat"

    @property
    def current_option(self) -> str | None:
        repeat = current_plan(self.coordinator, self.pile_code)["repeat"]
        return repeat if repeat in OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        if option not in OPTIONS:
            raise HomeAssistantError(f"不支持的重复方式: {option}")
        try:
            await async_apply_plan(self.coordinator, self.pile_code, repeat=option)
        except (PineconeError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err
