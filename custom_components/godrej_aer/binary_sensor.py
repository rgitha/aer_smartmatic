from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import async_device_device_info_fn
from .godrej import SmartMatic
from .godrej.events import (
    DEVICE_CONNECT,
    DEVICE_DISCONNECT,
    DEVICE_REACHABILITY,
    DEVICE_STATUS_UPDATE,
)

# Commands to one device are serialised by the device code itself.
PARALLEL_UPDATES = 0

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    instance: SmartMatic = config_entry.runtime_data
    async_add_entities([
        SmartMaticReachableBinarySensor(instance, config_entry),
        SmartMaticBluetoothLinkBinarySensor(instance, config_entry),
    ])


class SmartMaticBinarySensor(BinarySensorEntity):
    _events: tuple[str, ...] = ()
    _attr_has_entity_name = True

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        self._instance: SmartMatic = instance
        self._config_entry = config_entry
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self) -> None:
        for event in self._events:
            self._instance.eventbus.add_listener(event, self.on_event)

    async def async_will_remove_from_hass(self) -> None:
        for event in self._events:
            self._instance.eventbus.remove_listener(event, self.on_event)

    @property
    def device_info(self) -> DeviceInfo:
        return async_device_device_info_fn(self._instance, self._config_entry.data[CONF_NAME])

    async def on_event(self, _data: object) -> None:
        self.async_write_ha_state()


class SmartMaticReachableBinarySensor(SmartMaticBinarySensor):
    """OK / Unreachable. Unreachable only on real trouble: a failed exchange,
    or no advertisements. The link is dropped after every exchange to save
    battery, so that says nothing about the device's health.
    """

    _events = (DEVICE_REACHABILITY, DEVICE_STATUS_UPDATE)
    _attr_translation_key = "status"

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        super().__init__(instance, config_entry)
        self._attr_unique_id = f"{config_entry.entry_id}_reachable"

    @property
    def is_on(self) -> bool | None:
        return self._instance.reachable

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        # An attribute rather than a timestamp sensor: attribute changes
        # don't add activity log entries.
        last = self._instance.last_update
        return {"last_update": last.isoformat() if last else None}


class SmartMaticBluetoothLinkBinarySensor(SmartMaticBinarySensor):
    """Whether a Bluetooth link is open right now (Active/Idle).

    Normal operation opens and closes it constantly, so it is disabled by
    default; enable it to debug connections.
    """

    _events = (DEVICE_CONNECT, DEVICE_DISCONNECT)
    _attr_translation_key = "bluetooth_link"
    _attr_entity_registry_enabled_default = False

    def __init__(self, instance: SmartMatic, config_entry: ConfigEntry):
        super().__init__(instance, config_entry)
        self._attr_unique_id = f"{config_entry.entry_id}_bluetooth_link"

    @property
    def is_on(self) -> bool:
        client = self._instance.client
        return bool(client and client.is_connected)

