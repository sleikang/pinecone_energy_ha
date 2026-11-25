"""Base entity for 松果能源 charging piles."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PineconeCoordinator


class PineconeEntity(CoordinatorEntity[PineconeCoordinator]):
    """Common behaviour for every pile backed entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: PineconeCoordinator, pile_code: str) -> None:
        super().__init__(coordinator)
        self.pile_code = pile_code
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pile_code)},
            manufacturer="松果能源",
            name=coordinator.device_name(pile_code),
            model="交流充电桩" if coordinator.pile_type(pile_code) == "ac" else "直流充电桩",
            serial_number=pile_code,
        )

    @property
    def data(self) -> dict[str, Any]:
        return self.coordinator.details(self.pile_code)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = self.data
        return bool(data) and data.get("online", True)

    @property
    def charging(self) -> dict[str, Any]:
        return self.data.get("charging") or {}

    @property
    def status_code(self) -> int | None:
        return self.data.get("status_code")

    @property
    def status_text(self) -> str:
        return str(self.data.get("status") or "未知")

    def _source(self) -> dict[str, Any]:
        """Detail payload of the pile (pile/query/one)."""
        return self.data.get("detail") or {}
