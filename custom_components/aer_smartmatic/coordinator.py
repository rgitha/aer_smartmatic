"""The Smart Matic's coordinator: when to read it, and its entities' updates.

Home Assistant's Bluetooth coordinator calls ``needs_poll`` on each of the
device's advertisements. A status read is due when none succeeded in the
last hour, or the last exchange failed (with a back-off after failures);
the device code decides that, and does the reading.

The coordinator also reads the status around each schedule's start and
end, so a run starting or ending shows without waiting for the hourly read,
and runs Update status (the action, the button and update_entity), sharing
one read when several ask at once.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .godrej import SmartMatic
from .godrej.events import (
    DEVICE_CONNECT,
    DEVICE_DISCONNECT,
    DEVICE_REACHABILITY,
    DEVICE_SCHEDULES_UPDATE,
    DEVICE_STATUS_UPDATE,
)
from .godrej.schedule import next_change

_LOGGER = logging.getLogger(__name__)

# The device code's events; each updates the entities.
_EVENTS = (
    DEVICE_STATUS_UPDATE, DEVICE_REACHABILITY, DEVICE_SCHEDULES_UPDATE,
    DEVICE_CONNECT, DEVICE_DISCONNECT,
)


class SmartMaticCoordinator(ActiveBluetoothDataUpdateCoordinator[None]):
    """Reads one Smart Matic when due, and tells its entities of changes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, instance: SmartMatic) -> None:
        super().__init__(
            hass,
            _LOGGER,
            address=instance.mac,
            mode=bluetooth.BluetoothScanningMode.PASSIVE,
            needs_poll_method=self._needs_poll,
            poll_method=self._poll,
            connectable=True,
        )
        self.config_entry = entry
        self.instance = instance
        self._update_now: asyncio.Task[None] | None = None
        self._next_change: CALLBACK_TYPE | None = None

    # --- lifecycle

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start following advertisements and the device's own events."""
        stop_bluetooth = super().async_start()
        for event in _EVENTS:
            self.instance.eventbus.add_listener(event, self._on_device_event)
        self.instance.eventbus.add_listener(DEVICE_SCHEDULES_UPDATE, self._on_schedules)
        self._plan_next_change()

        @callback
        def _stop() -> None:
            stop_bluetooth()
            for event in _EVENTS:
                self.instance.eventbus.remove_listener(event, self._on_device_event)
            self.instance.eventbus.remove_listener(DEVICE_SCHEDULES_UPDATE, self._on_schedules)
            self._cancel_next_change()
            if self._update_now is not None:
                self._update_now.cancel()

        return _stop

    async def _on_device_event(self, _data: object) -> None:
        self.async_update_listeners()

    async def _on_schedules(self, _data: object) -> None:
        # A new or changed list moves the next start or end.
        self._plan_next_change()

    # --- advertisements and background reads

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        # Not the base class's: entities don't need writing on every
        # advertisement (several a second); they follow the device's
        # events, including it being heard again.
        self._available = True
        self._last_service_info = service_info
        self.instance.set_advertising(True)
        if self.needs_poll(service_info):
            self._debounced_poll.async_schedule_call()

    @callback
    def _async_handle_unavailable(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        # No advertisements for a while: out of range or out of battery.
        super()._async_handle_unavailable(service_info)
        self.instance.set_advertising(False)

    def _needs_poll(
        self, _service_info: bluetooth.BluetoothServiceInfoBleak, _last_poll: float | None
    ) -> bool:
        return self.instance.needs_status_read()

    async def _poll(self, _service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        try:
            await self.instance.connect_if_needed()
        except Exception:  # noqa: BLE001 - logged, and backed off, by the device code
            return

    # --- Update status

    async def async_update_now(self) -> None:
        """Read the status and schedules now (Update status, update_entity).

        Callers at the same time share one read. Raises if the status
        can't be read; the schedules are read as a courtesy.
        """
        task = self._update_now
        if task is None or task.done():
            task = self.hass.async_create_task(self._read_now())
            self._update_now = task
        await asyncio.shield(task)

    async def _read_now(self) -> None:
        await self.instance.connect(purpose="update status")
        await self.instance.read_schedules_quietly()

    # --- schedule starts and ends

    @callback
    def _plan_next_change(self) -> None:
        """Read the status after the next schedule start or end."""
        self._cancel_next_change()
        schedules = self.instance.schedules
        if not schedules:
            return
        upcoming = next_change(schedules, dt_util.now())
        if upcoming is None:
            return
        when, starting = upcoming
        _LOGGER.debug(
            "Next schedule %s on %s: %s", "start" if starting else "end",
            self.instance.mac, when.isoformat(timespec="minutes"),
        )

        @callback
        def _at_change(_now: datetime) -> None:
            self._next_change = None
            if self.instance.heard:
                self.instance.watch_schedule_change(starting=starting)
            self._plan_next_change()

        self._next_change = async_track_point_in_time(self.hass, _at_change, when)

    @callback
    def _cancel_next_change(self) -> None:
        if self._next_change is not None:
            self._next_change()
            self._next_change = None



def coordinator_for(entry: ConfigEntry) -> SmartMaticCoordinator | None:
    """The entry's coordinator, if it is loaded."""
    data = getattr(entry, "runtime_data", None)
    return data if isinstance(data, SmartMaticCoordinator) else None

