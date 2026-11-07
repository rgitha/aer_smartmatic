from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SmartMaticCoordinator
from .entity import SmartMaticEntity

# Commands to one device are serialised by the device code itself.
PARALLEL_UPDATES = 0

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartMaticCoordinator = config_entry.runtime_data
    async_add_entities([
        SmartMaticReachableBinarySensor(coordinator),
        SmartMaticBluetoothLinkBinarySensor(coordinator),
    ])


class SmartMaticBinarySensor(SmartMaticEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        # They report the device's state, including not being heard.
        return True


class SmartMaticReachableBinarySensor(SmartMaticBinarySensor):
    """OK / Unreachable. Unreachable only on real trouble: a failed exchange,
    or no advertisements. The link is dropped after every exchange to save
    battery, so that says nothing about the device's health.
    """

    _key = "reachable"
    _attr_translation_key = "status"

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

    _key = "bluetooth_link"
    _attr_translation_key = "bluetooth_link"
    _attr_entity_registry_enabled_default = False

    @property
    def is_on(self) -> bool:
        client = self._instance.client
        return bool(client and client.is_connected)

