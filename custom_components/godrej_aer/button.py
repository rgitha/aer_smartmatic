from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_NAME, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import async_device_device_info_fn, async_send_command
from .godrej import SmartMatic
from .godrej.events import DEVICE_REACHABILITY


_LOGGER = logging.getLogger(__name__)
# Commands to one device are serialised by the device code itself.
PARALLEL_UPDATES = 0

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([
        SmartMaticSprayNowButton(instance, config_entry),
        SmartMaticResetRefillButton(instance, config_entry),
        SmartMaticUpdateStatusButton(instance, config_entry),
        SmartMaticIdentifyButton(instance, config_entry),
    ])


class SmartMaticButton(ButtonEntity):
    """Base for Smart Matic buttons."""

    _key: str
    _attr_has_entity_name = True

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance: SmartMatic = instance
        self._config_entry: ConfigEntry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_{self._key}"

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(self._instance, self._config_entry.data[CONF_NAME])


class SmartMaticDeviceButton(SmartMaticButton):
    """A button that acts on the device: unavailable while it isn't heard."""

    @property
    def available(self) -> bool:
        return self._instance.heard

    async def async_added_to_hass(self) -> None:
        self._instance.eventbus.add_listener(DEVICE_REACHABILITY, self._on_reachability)

    async def async_will_remove_from_hass(self) -> None:
        self._instance.eventbus.remove_listener(DEVICE_REACHABILITY, self._on_reachability)

    async def _on_reachability(self, _instance: SmartMatic) -> None:
        self.async_write_ha_state()


class SmartMaticSprayNowButton(SmartMaticDeviceButton):
    """Spray once, now."""

    _key = "trigger"
    _attr_translation_key = "spray_now"

    async def async_press(self) -> None:
        _LOGGER.debug("%s: Spray now pressed", self._config_entry.data[CONF_NAME])
        await async_send_command(self._instance.spray_now())


class SmartMaticResetRefillButton(SmartMaticDeviceButton):
    """Tell the device its cartridge has been refilled."""

    _key = "reset_refill"
    _attr_translation_key = "reset_refill"
    # Its own card on the device page, away from the everyday controls.
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        _LOGGER.debug("%s: Reset refill level pressed", self._config_entry.data[CONF_NAME])
        await async_send_command(self._instance.reset_refill())


class SmartMaticUpdateStatusButton(SmartMaticButton):
    """Read the status and schedules now, like the Update status action."""

    _key = "update_status"
    _attr_translation_key = "update_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        _LOGGER.debug("%s: Update status pressed", self._config_entry.data[CONF_NAME])
        await async_send_command(self._update())

    async def _update(self) -> None:
        await self._instance.connect(purpose="update status")
        # A firmware that never answers the schedule request still gets
        # its status updated.
        await self._instance.read_schedules_quietly()


class SmartMaticIdentifyButton(SmartMaticDeviceButton):
    """Light the device's three interval lights for a few seconds (HA's
    standard identify button: the type only sets its icon and name)."""

    _key = "identify"
    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        _LOGGER.debug("%s: Identify pressed", self._config_entry.data[CONF_NAME])
        await async_send_command(self._instance.identify())
