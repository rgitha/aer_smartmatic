from __future__ import annotations

from collections.abc import Awaitable

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH

from .const import DOMAIN
from .godrej import SmartMatic
from .godrej.exception import (
    BatteryTooLow, CommandNotConfirmed, ConnectionError, NotIdle, SchedulesChanged,
    SchedulesNotStored,
)


def device_name(hass: HomeAssistant, instance: SmartMatic) -> str:
    """The device's name as the user sees it (renamed or not).

    Found through its config entry, which has the one device: looking it
    up by its Bluetooth connection is deprecated (Home Assistant 2026.8),
    and this works on older versions too.
    """
    registry = dr.async_get(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if getattr(entry, "runtime_data", None) is instance:
            for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
                return device.name_by_user or device.name or instance.mac
    return instance.mac


def update_failed(names: str) -> HomeAssistantError:
    """Update status failed: "Couldn't reach <names>. ..." """
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="update_failed",
        translation_placeholders={"names": names},
    )


class AcceptedButFlat(HomeAssistantError):
    """The device took the command, but its batteries are too low to spray."""


async def async_send_command(command: Awaitable[object]) -> None:
    """Run a device command, turning failures into a translated HA error.

    The frontend shows it as a toast on the control that was used.
    """
    try:
        await command
    except BatteryTooLow as err:
        raise AcceptedButFlat(
            translation_domain=DOMAIN,
            translation_key="battery_too_low",
        ) from err
    except SchedulesNotStored as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="schedules_not_stored",
        ) from err
    except SchedulesChanged as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="schedules_changed",
        ) from err
    except NotIdle as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="schedule_running" if err.schedule else "auto_running",
        ) from err
    except ConnectionError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_reach",
        ) from err
    except CommandNotConfirmed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_not_confirmed",
        ) from err


@callback
def async_device_device_info_fn(smartmatic: SmartMatic, name: str) -> DeviceInfo:
    status = smartmatic.device_status
    return DeviceInfo(
        connections={(CONNECTION_BLUETOOTH, smartmatic.mac)},
        manufacturer="Godrej Aer",
        model="Smart Matic",
        name=name,
        sw_version=status.firmware_version if status else None,
    )
