from __future__ import annotations

import time
from datetime import datetime
from collections.abc import Callable

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import dt as dt_util

from .coordinator import SmartMaticCoordinator
from .entity import SmartMaticEntity
from .godrej import DeviceStatus
from .godrej.schedule import Schedule


# Commands to one device are serialised by the device code itself.
PARALLEL_UPDATES = 0

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartMaticCoordinator = config_entry.runtime_data
    instance = coordinator.instance
    async_add_entities([
        SmartMaticStatusSensor(coordinator),
        SmartMaticBatterySensor(coordinator),
        SmartMaticBatteryVoltageSensor(coordinator),
        SmartMaticRefillLevelSensor(coordinator),
        SmartMaticSprayCountSensor(coordinator),
        SmartMaticChannelSensor(coordinator, "command_channel", instance.command_char),
        SmartMaticChannelSensor(coordinator, "reply_channel", instance.reply_char),
        SmartMaticSignalStrengthSensor(coordinator),
        # Schedules need the device's clock.
        *([SmartMaticSchedulesSensor(coordinator)] if instance.has_clock else []),
        SmartMaticUpdatedLastSensor(coordinator),
        SmartMaticBatteriesReplacedSensor(coordinator),
    ])

class SmartMaticSensor(SmartMaticEntity, SensorEntity):
    """Base for sensors showing a value from the last status read."""

    _value_fn: Callable[[DeviceStatus], float | int | None]

    @property
    def native_value(self) -> float | int | None:
        status = self._instance.device_status
        return self._value_fn(status) if status is not None else None


class SmartMaticStatusSensor(SmartMaticEntity, SensorEntity):
    """One status: Idle, Auto, Schedule or Disconnected.

    Disconnected when Connection status is Unreachable (not heard, or the
    last exchange failed); otherwise the mode from the last status read
    (see Updated last). Unknown until something is heard or read.
    """

    _key = "device_status"
    _attr_translation_key = "device_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "auto", "schedule", "disconnected"]

    @property
    def available(self) -> bool:
        # Shows Disconnected rather than becoming unavailable.
        return True

    @property
    def native_value(self) -> str | None:
        if self._instance.reachable is False:
            return "disconnected"
        status = self._instance.device_status
        if status is None or status.current_status is None:
            return None
        if not status.is_spraying:
            return "idle"
        return "schedule" if status.spray_source == "schedule" else "auto"

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        # Found at setup: without a clock the device keeps no schedules.
        return {"has_clock": self._instance.has_clock}


class SmartMaticBatterySensor(SmartMaticSensor):
    """Estimated battery level (linear in the voltage; see const.py)."""

    _key = "battery_level"
    _attr_translation_key = "battery_level"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _value_fn = staticmethod(lambda status: status.battery_percent)


class SmartMaticBatteryVoltageSensor(SmartMaticSensor):
    """The measured voltage the battery level is estimated from."""

    _key = "battery_voltage"
    _attr_translation_key = "battery_voltage"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.MILLIVOLT
    _attr_suggested_display_precision = 0
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # The Battery sensor shows the level; enable this for the raw voltage.
    _attr_entity_registry_enabled_default = False
    _value_fn = staticmethod(lambda status: status.battery_mv)


class SmartMaticRefillLevelSensor(SmartMaticSensor):
    _key = "refill_level"
    _attr_translation_key = "refill_level"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _value_fn = staticmethod(lambda status: status.refill_percent)


class SmartMaticSprayCountSensor(SmartMaticSensor):
    _key = "spray_count"
    _attr_translation_key = "spray_count"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _value_fn = staticmethod(lambda status: status.total_spray_count)


class SmartMaticChannelSensor(SmartMaticEntity, SensorEntity):
    """A detected characteristic, fixed until the device is reconfigured."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Only needed for troubleshooting: enable it on the device page.
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: SmartMaticCoordinator, key: str, uuid: str) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._attr_native_value = uuid[:8] if uuid else "not detected"
        self._attr_extra_state_attributes = {"uuid": uuid}

    @property
    def available(self) -> bool:
        return True


class SmartMaticSignalStrengthSensor(SmartMaticEntity, SensorEntity):
    """RSSI of the device's advertisements, and which adapter or proxy heard them."""

    _key = "signal_strength"
    _attr_translation_key = "signal_strength"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    # Advertisements arrive several times a second; record changes only.
    _MIN_INTERVAL = 60
    _MIN_CHANGE = 5

    def __init__(self, coordinator: SmartMaticCoordinator) -> None:
        super().__init__(coordinator)
        self._last_write = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        mac = self._instance.mac
        info = bluetooth.async_last_service_info(self.hass, mac, connectable=True)
        if info is not None:
            self._update(info)
            self.async_write_ha_state()
        self.async_on_remove(
            bluetooth.async_register_callback(
                self.hass,
                self._on_advertisement,
                BluetoothCallbackMatcher({ADDRESS: mac}),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )

    @callback
    def _on_advertisement(
        self,
        info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        previous = self._attr_native_value
        if (
            isinstance(previous, int)
            and info.source == (self._attr_extra_state_attributes or {}).get("source")
            and abs(info.rssi - previous) < self._MIN_CHANGE
            and time.monotonic() - self._last_write < self._MIN_INTERVAL
        ):
            return
        self._update(info)
        self.async_write_ha_state()

    def _update(self, info: bluetooth.BluetoothServiceInfoBleak) -> None:
        scanner = bluetooth.async_scanner_by_source(self.hass, info.source)
        self._attr_native_value = info.rssi
        self._attr_extra_state_attributes = {
            "source": info.source,
            "source_name": getattr(scanner, "name", None),
        }
        self._last_write = time.monotonic()



class SmartMaticSchedulesSensor(SmartMaticEntity, SensorEntity, RestoreEntity):
    """How many schedules the device holds, with the list as attributes.

    Read back from the device: once after startup, and after every change.
    Attributes: ``schedules`` (the list), ``running`` (the number of the
    schedule running now, or None) and ``last_read``. After a restart the
    last list is restored until the device is read again.
    """

    _key = "schedules"
    _attr_name = "Schedules"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: SmartMaticCoordinator) -> None:
        super().__init__(coordinator)
        self._update_from(self._instance.schedules)

    def _update_from(self, schedules: list[Schedule] | None) -> None:
        if schedules is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        read_at = self._instance.schedules_read_at
        self._attr_native_value = len(schedules)
        self._attr_extra_state_attributes = {
            "schedules": [
                {"number": i, **schedule.as_dict()}
                for i, schedule in enumerate(schedules, start=1)
            ],
            "running": self._instance.running_schedule(),
            "last_read": read_at.isoformat() if read_at else None,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._instance.schedules is None and (last := await self.async_get_last_state()):
            # Until the device is read: the list as last read, not running.
            try:
                self._attr_native_value = int(last.state)
            except (TypeError, ValueError):
                self._attr_native_value = None
            if self._attr_native_value is not None:
                self._attr_extra_state_attributes = {
                    "schedules": last.attributes.get("schedules", []),
                    "running": None,
                    "last_read": last.attributes.get("last_read"),
                }

    @callback
    def _handle_coordinator_update(self) -> None:
        # Until the device is read, keep the restored list.
        if self._instance.schedules is not None:
            self._update_from(self._instance.schedules)
        super()._handle_coordinator_update()


class SmartMaticUpdatedLastSensor(SmartMaticEntity, SensorEntity):
    """When the last status read succeeded.

    Named to sort next to the Update status button on the device page.
    """

    # Not "last_update": that was an older entity, removed on upgrade.
    _key = "updated_last"
    _attr_translation_key = "updated_last"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> datetime | None:
        return self._instance.last_update


class _BatteryLevel(ExtraStoredData):
    """The battery level last read, kept over a restart."""

    def __init__(self, percent: int | None) -> None:
        self.percent = percent

    def as_dict(self) -> dict[str, int | None]:
        return {"percent": self.percent}


class SmartMaticBatteriesReplacedSensor(SmartMaticEntity, SensorEntity, RestoreEntity):
    """When new batteries were last seen: the level rose by more than 10
    points between two status reads, to 90 % or more. Unknown until then.

    Kept over a restart, along with the last battery level, so a swap
    made while Home Assistant was down is seen at the first read.
    """

    _key = "batteries_replaced"
    _attr_translation_key = "batteries_replaced"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> datetime | None:
        return self._instance.batteries_replaced_at

    @property
    def extra_restore_state_data(self) -> _BatteryLevel:
        status = self._instance.device_status
        percent = status.battery_percent if status is not None else None
        return _BatteryLevel(percent if percent is not None else self._instance.restored_battery_percent)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        instance = self._instance
        if instance.batteries_replaced_at is None and (last := await self.async_get_last_state()):
            instance.batteries_replaced_at = dt_util.parse_datetime(last.state)
        if instance.device_status is None and (extra := await self.async_get_last_extra_data()):
            percent = extra.as_dict().get("percent")
            if isinstance(percent, int):
                instance.restored_battery_percent = percent
