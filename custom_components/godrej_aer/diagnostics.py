"""Diagnostics download for a Smart Matic: what the integration knows."""
from __future__ import annotations

from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .godrej import SmartMatic

# The address identifies the device, and the name is usually a room.
TO_REDACT = {CONF_MAC, CONF_NAME, "address", "title"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a report for one device's config entry."""
    instance: SmartMatic = entry.runtime_data
    integration = await async_get_integration(hass, DOMAIN)
    status = instance.device_status
    info = bluetooth.async_last_service_info(hass, instance.mac, connectable=True)

    return {
        "integration_version": str(integration.version),
        "entry": async_redact_data(
            {"title": entry.title, "data": dict(entry.data), "options": dict(entry.options)},
            TO_REDACT,
        ),
        "channels": {
            "command": instance.command_char,
            "reply": instance.reply_char,
        },
        "status": None if status is None else {
            "firmware": status.firmware_version,
            "structure_version": status.structure_version,
            "mode_code": status.current_status,
            "spray_interval": status.spray_interval,
            "started_by": status.spray_source,
            "battery_mv": status.battery_mv,
            "battery_percent": status.battery_percent,
            "spray_counts": status.spray_counts,
            "total_sprays": status.total_spray_count,
            "refill_percent": status.refill_percent,
            "read_at": status.date.isoformat(),
            "raw": status.raw.hex(),
        },
        "connection": {
            "reachable": instance.reachable,
            "heard": instance.heard,
            "advertising": instance._advertising,
            "last_exchange_ok": instance._last_exchange_ok,
            "link_open": bool(instance.client and instance.client.is_connected),
            "last_update": instance.last_update.isoformat() if instance.last_update else None,
            "poll_failures": instance._poll_failures,
            "retry_in_seconds": instance.retry_delay,
        },
        "advertisement": None if info is None else {
            "rssi": info.rssi,
            "source": info.source,
            "connectable": info.connectable,
        },
    }
