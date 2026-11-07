"""The Aer Smart Matic integration."""
from __future__ import annotations
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, CONF_MAC
from homeassistant.core import HomeAssistant
from homeassistant.components import bluetooth
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import CONF_COMMAND_CHAR, CONF_HAS_CLOCK, CONF_REPLY_CHAR
from .const import DOMAIN
from .coordinator import SmartMaticCoordinator
from .godrej import SmartMatic
from .godrej.events import DEVICE_STATUS_UPDATE
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)
# Replaced by the "Spray: auto" select (Off / every 10, 20 or 40 min).
_REPLACED_ENTITIES = (("switch", "auto_spray"), ("select", "spray_interval"))

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
]

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions, once."""
    async_setup_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry
) -> bool:
    """Set up Aer Smart Matic from a config entry."""

    mac = entry.options.get(CONF_MAC, None) or entry.data.get(CONF_MAC, None)

    instance = SmartMatic(
        hass,
        mac,
        command_char=entry.data.get(CONF_COMMAND_CHAR),
        reply_char=entry.data.get(CONF_REPLY_CHAR),
        has_clock=entry.data.get(CONF_HAS_CLOCK, True),
    )
    coordinator = SmartMaticCoordinator(hass, entry, instance)
    entry.runtime_data = coordinator

    async def _read_schedules() -> None:
        _LOGGER.debug("Reading the schedules of %s after startup", mac)
        try:
            await instance.read_schedules()
        except Exception:
            _LOGGER.debug("Could not read schedules from %s", mac, exc_info=True)

    async def _on_first_status(_status: object) -> None:
        # Read the device's own schedules once, after the first status
        # shows it is reachable; later changes read them back themselves.
        instance.eventbus.remove_listener(DEVICE_STATUS_UPDATE, _on_first_status)
        if instance.schedules is None and instance.has_clock:
            entry.async_create_background_task(
                hass, _read_schedules(), f"aer_smartmatic schedules {mac}"
            )

    instance.eventbus.add_listener(DEVICE_STATUS_UPDATE, _on_first_status)
    entry.async_on_unload(
        lambda: instance.eventbus.remove_listener(DEVICE_STATUS_UPDATE, _on_first_status)
    )

    registry = er.async_get(hass)
    if not instance.has_clock:
        # Found to have no clock (Reconfigure): no schedules to show.
        old = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_schedules")
        if old:
            registry.async_remove(old)
    for platform, key in _REPLACED_ENTITIES:
        old = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        if old:
            registry.async_remove(old)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    # Reads start with the device's next advertisement.
    entry.async_on_unload(coordinator.async_start())

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: SmartMaticCoordinator = entry.runtime_data
        await coordinator.instance.shutdown()
    return unload_ok

async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Let Home Assistant offer the device again straight away.

    Otherwise it is offered only after a restart or a gap in its
    advertisements, whichever way the entry was added."""
    bluetooth.async_rediscover_address(hass, entry.data[CONF_MAC])


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.debug("Config entry %s updated", entry.entry_id)