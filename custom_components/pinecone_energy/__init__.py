"""The 松果能源 (Pinecone Energy) charging pile integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PineconeAuthError, PineconeClient, PineconeError
from .const import (
    CONF_PHONE,
    CONF_PILES,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import PineconeCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.TIME,
    Platform.SELECT,
]

CONF_SCAN_INTERVAL = "scan_interval"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a 松果能源 account."""
    session = async_get_clientsession(hass)
    client = PineconeClient(
        session,
        token=entry.data.get(CONF_TOKEN),
        phone=entry.data.get(CONF_PHONE),
    )
    piles = list(entry.data.get(CONF_PILES) or [])
    if not piles:
        raise ConfigEntryAuthFailed("没有选择任何充电桩")

    interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    coordinator = PineconeCoordinator(
        hass, client, piles, scan_interval=interval, config_entry=entry
    )
    coordinator.entry = entry

    try:
        await coordinator.async_config_entry_first_refresh()
    except PineconeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except PineconeError as err:
        _LOGGER.error("初始化松果能源失败: %s", err)
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Drop entities that no longer exist in the integration (e.g. the 电池电量
    # sensor, which only mirrored a static pile-side value and read 0% while
    # charging). Without this HA keeps a permanently unavailable orphan.
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id.endswith(("_soc", "_battery_level")):
            _LOGGER.info("移除已废弃的实体 %s", entity.entity_id)
            registry.async_remove(entity.entity_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
