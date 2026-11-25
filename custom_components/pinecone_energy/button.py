"""Buttons for 松果能源 charging piles."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import PineconeError
from .const import DOMAIN
from .coordinator import PineconeCoordinator
from .entity import PineconeEntity

BUTTONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key="unlock_gun",
        name="解锁充电枪",
        icon="mdi:lock-open-variant",
    ),
    ButtonEntityDescription(
        key="refresh",
        name="刷新数据",
        icon="mdi:refresh",
    ),
    ButtonEntityDescription(
        key="relogin",
        name="重新登录（短信验证码）",
        icon="mdi:account-key",
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
                PineconeButton(coordinator, code, description)
                for code in new_codes
                for description in BUTTONS
            ]
        )

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class PineconeButton(PineconeEntity, ButtonEntity):
    entity_description: ButtonEntityDescription

    def __init__(
        self,
        coordinator: PineconeCoordinator,
        pile_code: str,
        description: ButtonEntityDescription,
    ) -> None:
        super().__init__(coordinator, pile_code)
        self.entity_description = description
        self._attr_unique_id = f"{pile_code}_{description.key}"
        self._attr_translation_key = description.key

    @property
    def available(self) -> bool:
        # The re-login button must stay usable precisely when auth has failed.
        if self.entity_description.key == "relogin":
            return self.coordinator.entry is not None
        return super().available

    async def async_press(self) -> None:
        key = self.entity_description.key
        try:
            if key == "unlock_gun":
                await self.coordinator.client.unlock_gun(self.pile_code)
            elif key == "refresh":
                await self.coordinator.async_request_refresh()
                return
            elif key == "relogin":
                self.coordinator.entry.async_start_reauth(self.hass)
                return
        except PineconeError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
