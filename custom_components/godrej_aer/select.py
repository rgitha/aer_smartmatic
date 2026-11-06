from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SmartMaticCoordinator
from .entity import AcceptedButFlat, SmartMaticEntity, async_send_command
from .godrej import DeviceStatus
from .godrej.exception import ScheduleRunning
from .godrej.const import SPRAY_INTERVALS

OFF = "Off"
_INTERVAL_BY_OPTION = {f"Every {minutes} min": minutes for minutes in SPRAY_INTERVALS}
_OPTION_BY_INTERVAL = {minutes: option for option, minutes in _INTERVAL_BY_OPTION.items()}


_LOGGER = logging.getLogger(__name__)
# Commands to one device are serialised by the device code itself.
PARALLEL_UPDATES = 0

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartMaticCoordinator = config_entry.runtime_data
    async_add_entities([SmartMaticAutoSpraySelect(coordinator)])


class SmartMaticAutoSpraySelect(SmartMaticEntity, SelectEntity):
    """Auto-spray mode: Off, or every 10, 20 or 40 minutes.

    One setting, as on the device. The state is what the device reports,
    and "started_by" says what started auto-spraying. Choosing the
    interval already running sends nothing: the device would restart the
    cycle with a spray. Off is always sent (it also clears the lights).
    """

    _key = "spray_mode"
    _attr_translation_key = "spray_mode"
    _attr_options = [OFF, *_INTERVAL_BY_OPTION]

    def __init__(self, coordinator: SmartMaticCoordinator) -> None:
        super().__init__(coordinator)
        self._applied: DeviceStatus | None = None
        self._apply_status(self._instance.device_status)

    def _apply_status(self, status: DeviceStatus | None) -> None:
        self._applied = status
        if status is None or status.current_status is None:
            self._attr_current_option = None
            self._attr_extra_state_attributes = {}
            return
        if status.is_spraying:
            self._attr_current_option = _OPTION_BY_INTERVAL.get(status.spray_interval)
        else:
            self._attr_current_option = OFF
        self._attr_extra_state_attributes = {"started_by": status.spray_source}

    @callback
    def _handle_coordinator_update(self) -> None:
        # Only a new status changes the option: after a choice is
        # acknowledged it stays shown until the status read-back.
        if self._instance.device_status is not self._applied:
            self._apply_status(self._instance.device_status)
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        _LOGGER.debug("%s: Spray: auto set to %s", self._config_entry.data[CONF_NAME], option)
        # Judged on a fresh status, read just before in the same step: the
        # device refuses Auto during a schedule run (Off only pauses one;
        # Skip is the way to stop it), and an interval already running
        # isn't re-sent (that would restart it with an extra spray).
        command = self._instance.choose_auto(
            None if option == OFF else _INTERVAL_BY_OPTION[option]
        )
        # Show the option once the device acknowledges it; otherwise
        # re-write the current one (the dropdown reverts) and raise. The
        # status read-back that follows shows what the device really does
        # (e.g. it ignores a different interval during a schedule run).
        try:
            await async_send_command(command)
        except ScheduleRunning as err:
            self.async_write_ha_state()
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auto_during_schedule"
            ) from err
        except AcceptedButFlat:
            # The device did switch modes; it just can't spray.
            self._attr_current_option = option
            self.async_write_ha_state()
            raise
        except Exception:
            self.async_write_ha_state()
            raise
        self._attr_current_option = option
        self.async_write_ha_state()
