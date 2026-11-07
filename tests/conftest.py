"""Test setup: stub the few Home Assistant modules the device code imports.

The ``godrej`` package only needs a few Bluetooth lookups
(``async_ble_device_from_address``, ``async_last_service_info``,
``async_scanner_by_source``), ``dt_util.now`` and ``HomeAssistantError``
from Home Assistant, so the tests run without installing it.
"""
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


def _stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules.setdefault(name, module)
    return module


class HomeAssistantError(Exception):
    pass


_stub("homeassistant")
_stub("homeassistant.components")
_stub(
    "homeassistant.components.bluetooth",
    async_ble_device_from_address=lambda hass, mac: object(),
    async_last_service_info=lambda hass, mac, connectable=True: None,
    async_scanner_by_source=lambda hass, source: None,
)
_stub("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
_stub("homeassistant.util")
_stub(
    "homeassistant.util.dt",
    now=lambda: datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
    utcnow=lambda: datetime.now(timezone.utc),
)
sys.modules["homeassistant.util"].dt = sys.modules["homeassistant.util.dt"]

# Import the device library as a top-level package so the integration's
# __init__ (which needs the real Home Assistant) is not loaded.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "aer_smartmatic"))
