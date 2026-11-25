"""Sensors for 松果能源 charging piles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import PineconeCoordinator
from .entity import PineconeEntity


def _charging_value(key: str) -> Callable[[PineconeEntity], Any]:
    return lambda entity: (entity.charging or {}).get(key)


def _stats_value(key: str) -> Callable[[PineconeEntity], Any]:
    return lambda entity: ((entity.data.get("stats") or {}) or {}).get(key)


def _period_value(period: str) -> Callable[[PineconeEntity], Any]:
    return lambda entity: ((entity.data.get(period) or {}) or {}).get("total_energy")


def _soc_value(entity: PineconeEntity) -> Any:
    """Live SOC when charging, otherwise the pile's last reported SOC."""
    value = (entity.charging or {}).get("soc")
    if value not in (None, ""):
        return value
    detail = entity.data.get("detail") or {}
    return detail.get("soc")


@dataclass(frozen=True, kw_only=True)
class PineconeSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PineconeEntity], Any] = lambda entity: None


SENSORS: tuple[PineconeSensorDescription, ...] = (
    PineconeSensorDescription(
        key="status",
        name="充电状态",
        icon="mdi:ev-station",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "空闲中",
            "已连接",
            "充电中",
            "预约充电中",
            "故障",
            "已离线",
            "等待充电中",
            "充电完成",
            "已充满",
            "充电暂停",
            "车辆预约中",
            "升级中",
            "待插枪",
            "充电模组已取下",
            "未知",
        ],
        value_fn=lambda entity: entity.status_text,
    ),
    PineconeSensorDescription(
        key="power",
        name="充电功率",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:flash",
        value_fn=_charging_value("power"),
    ),
    PineconeSensorDescription(
        key="session_energy",
        name="本次充电电量",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:battery-charging",
        value_fn=_charging_value("energy"),
    ),
    PineconeSensorDescription(
        key="session_duration",
        name="本次充电时长",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:timer-outline",
        value_fn=_charging_value("duration"),
    ),
    PineconeSensorDescription(
        key="soc",
        name="电池电量",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
        value_fn=_soc_value,
    ),
    PineconeSensorDescription(
        key="voltage",
        name="充电电压",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:flash-triangle",
        value_fn=_charging_value("voltage"),
    ),
    PineconeSensorDescription(
        key="current",
        name="充电电流",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:current-ac",
        value_fn=_charging_value("current"),
    ),
    PineconeSensorDescription(
        key="signal",
        name="4G 信号",
        native_unit_of_measurement="格",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal",
        value_fn=lambda entity: entity.data.get("signal"),
    ),
    PineconeSensorDescription(
        key="today_energy",
        name="今日充电电量",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:chart-bar",
        value_fn=_stats_value("total_energy"),
    ),
    PineconeSensorDescription(
        key="today_sessions",
        name="今日充电次数",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:counter",
        value_fn=_stats_value("total_count"),
    ),
    PineconeSensorDescription(
        key="today_duration",
        name="今日充电时长",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:timer-sand",
        value_fn=_stats_value("total_duration"),
    ),
    PineconeSensorDescription(
        key="week_energy",
        name="本周充电电量",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:calendar-week",
        value_fn=_period_value("week"),
    ),
    PineconeSensorDescription(
        key="month_energy",
        name="本月充电电量",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:calendar-month",
        value_fn=_period_value("month"),
    ),
    PineconeSensorDescription(
        key="year_energy",
        name="今年充电电量",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:calendar",
        value_fn=_period_value("year"),
    ),
    PineconeSensorDescription(
        key="order_code",
        name="当前订单号",
        icon="mdi:receipt-text-outline",
        entity_registry_enabled_default=False,
        value_fn=_charging_value("order_code"),
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
        entities = [
            PineconeSensor(coordinator, code, description)
            for code in new_codes
            for description in SENSORS
        ]
        async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class PineconeSensor(PineconeEntity, SensorEntity):
    entity_description: PineconeSensorDescription

    def __init__(
        self,
        coordinator: PineconeCoordinator,
        pile_code: str,
        description: PineconeSensorDescription,
    ) -> None:
        super().__init__(coordinator, pile_code)
        self.entity_description = description
        self._attr_unique_id = f"{pile_code}_{description.key}"
        self._attr_translation_key = description.key

    @property
    def native_value(self) -> Any:
        try:
            value = self.entity_description.value_fn(self)
        except (TypeError, ValueError, KeyError):
            return None
        if value in ("", None):
            return None
        return value
