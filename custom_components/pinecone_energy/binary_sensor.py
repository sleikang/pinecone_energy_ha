"""Binary sensors for 松果能源 charging piles."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import PineconeCoordinator
from .entity import PineconeEntity

DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="online",
        name="在线",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:wifi",
    ),
    BinarySensorEntityDescription(
        key="charging",
        name="正在充电",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:lightning-bolt",
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
                PineconeBinarySensor(coordinator, code, description)
                for code in new_codes
                for description in DESCRIPTIONS
            ]
        )

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class PineconeBinarySensor(PineconeEntity, BinarySensorEntity):
    entity_description: BinarySensorEntityDescription

    def __init__(
        self,
        coordinator: PineconeCoordinator,
        pile_code: str,
        description: BinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, pile_code)
        self.entity_description = description
        self._attr_unique_id = f"{pile_code}_{description.key}"
        self._attr_translation_key = description.key

    @property
    def is_on(self) -> bool | None:
        if self.entity_description.key == "online":
            return bool(self.data.get("online"))
        return self.status_code == 2 or self.status_text == "充电中"
