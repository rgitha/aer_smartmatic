from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_NAME, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SmartMaticCoordinator
from .entity import (
    SmartMaticEntity, async_send_command, device_name, update_failed,
)


_LOGGER = logging.getLogger(__name__)
# Commands to one device are serialised by the device code itself.
PARALLEL_UPDATES = 0

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SmartMaticCoordinator = config_entry.runtime_data
    async_add_entities([
        SmartMaticSprayNowButton(coordinator),
        SmartMaticResetRefillButton(coordinator),
        SmartMaticUpdateStatusButton(coordinator),
        SmartMaticIdentifyButton(coordinator),
    ])


class SmartMaticDeviceButton(SmartMaticEntity, ButtonEntity):
    """A button that acts on the device: unavailable while it isn't heard."""


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


class SmartMaticUpdateStatusButton(SmartMaticDeviceButton):
    """Read the status and schedules now, like the Update status action."""

    _key = "update_status"
    _attr_translation_key = "update_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        _LOGGER.debug("%s: Update status pressed", self._config_entry.data[CONF_NAME])
        # The same error as the Update status action, naming the device.
        try:
            await self._update()
        except Exception as err:
            _LOGGER.warning("Manual status update failed for %s: %s", self._instance.mac, err)
            raise update_failed(device_name(self.hass, self._instance)) from err

    async def _update(self) -> None:
        # The status, then the schedules (a firmware that never answers
        # the schedule request still gets its status updated).
        await self.coordinator.async_update_now()


class SmartMaticIdentifyButton(SmartMaticDeviceButton):
    """Light the device's three interval lights for a few seconds (HA's
    standard identify button: the type only sets its icon and name)."""

    _key = "identify"
    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        _LOGGER.debug("%s: Identify pressed", self._config_entry.data[CONF_NAME])
        await async_send_command(self._instance.identify())
