"""Config flow, reconfigure, entry setup and sensors against Home Assistant."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.godrej_aer.const import CONF_COMMAND_CHAR, CONF_REPLY_CHAR, DOMAIN
from custom_components.godrej_aer.godrej.exception import (
    ConnectionError as DeviceConnectionError,
    InvalidDeviceError,
)

MAC = "A4:C1:38:CB:03:37"
C1 = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
C3 = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
C4 = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
CF = "custom_components.godrej_aer.config_flow"
DETECT = f"{CF}.SmartMatic.detect_channels"
UNAVAILABLE = []


def deliver_status(instance, frame):
    """A status reply arriving for a read in progress (the device copies
    the read's number into it); one nobody asked for is ignored."""
    from custom_components.godrej_aer.godrej import cbor
    from custom_components.godrej_aer.godrej.godrej import _Waiter

    mn = cbor.loads(frame)["mN"]
    instance._waiters[mn] = _Waiter(157, "Status request", asyncio.get_running_loop().create_future())
    try:
        instance._on_notification(C1, frame)
    finally:
        instance._waiters.pop(mn, None)


@pytest.fixture(autouse=True)
def no_deps():
    # Skip setting up the bluetooth_adapters dependency in the test hass.
    with patch("homeassistant.setup.async_process_deps_reqs", AsyncMock()), \
         patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock(), create=True):
        yield


def discovered():
    return [SimpleNamespace(address=MAC, name="Smart Matic")]


async def start_flow(hass):
    with patch(f"{CF}.async_discovered_service_info", return_value=discovered()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return result


def no_setup():
    """Creating the entry would set it up; not needed in flow tests."""
    return patch("custom_components.godrej_aer.async_setup_entry", AsyncMock(return_value=True))


async def pick_device(hass, result, detect):
    with patch(DETECT, detect), no_setup(), \
         patch(f"{CF}.SmartMatic.disconnect", AsyncMock()), \
         patch(f"{CF}.async_discovered_service_info", return_value=discovered()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"mac": MAC}
        )
        result, _ = await finish_progress(hass, result)
        return result


async def finish_progress(hass, result):
    """Let detection run and follow the flow until it leaves the progress steps."""
    seen = []
    while result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.SHOW_PROGRESS_DONE):
        if result["type"] is FlowResultType.SHOW_PROGRESS:
            seen.append((result["progress_action"], dict(result.get("description_placeholders") or {})))
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result, seen


async def test_setup_stores_detected_channels(hass):
    result = await start_flow(hass)
    detect = AsyncMock(return_value=(C4, C1))
    result = await pick_device(hass, result, detect)
    detect.assert_awaited_once()
    # Added straight away under the advertised name; Home Assistant's own
    # dialog then offers to rename it and pick an area.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Smart Matic"
    assert result["data"] == {
        "mac": MAC, "name": "Smart Matic", CONF_COMMAND_CHAR: C4, CONF_REPLY_CHAR: C1,
        "has_clock": True,
    }


async def test_setup_stores_a_missing_clock(hass):
    async def detect(self, **kwargs):
        self.detected_clock = False
        return C4, C1

    result = await start_flow(hass)
    result = await pick_device(hass, result, detect)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["has_clock"] is False


@pytest.mark.parametrize(
    ("detect", "error"),
    [
        (AsyncMock(return_value=None), "no_reply"),
        (AsyncMock(side_effect=DeviceConnectionError("x")), "cannot_connect"),
        (AsyncMock(side_effect=InvalidDeviceError("x")), "invalid_device"),
    ],
)
async def test_setup_without_detection_creates_nothing(hass, detect, error):
    result = await start_flow(hass)
    result = await pick_device(hass, result, detect)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"  # back to the device list, to retry
    assert result["errors"] == {"base": error}
    assert hass.config_entries.async_entries(DOMAIN) == []


def make_entry(hass, command_char=C4, reply_char=C1, **data):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC.lower(),
        data={
            "mac": MAC, "name": "Hall",
            CONF_COMMAND_CHAR: command_char, CONF_REPLY_CHAR: reply_char, **data,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def start_reconfigure(hass, entry):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    return result


async def test_reconfigure_reports_progress_and_detected_channels(hass):
    entry = make_entry(hass)
    result = await start_reconfigure(hass, entry)
    flow_id = result["flow_id"]
    probed, finish = asyncio.Event(), asyncio.Event()

    async def detect(self, *, progress=None, **kwargs):
        progress("connecting", {}, 0.0)
        await probed.wait()
        progress("probing", {"index": "2", "total": "3", "channel": "6e400003"}, 0.33)
        await finish.wait()
        return (C3, C1)

    async def settle():
        # Not async_block_till_done: it would wait for the gated detection.
        for _ in range(20):
            await asyncio.sleep(0)

    def shown():
        """What the dialog currently shows (HA advances the flow by itself)."""
        step = hass.config_entries.flow._progress[flow_id].cur_step
        return step["progress_action"], dict(step.get("description_placeholders") or {})

    with patch(DETECT, detect), \
         patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(flow_id, {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await settle()
        assert shown() == ("connecting", {})

        probed.set()
        await settle()
        assert shown() == ("probing", {"index": "2", "total": "3", "channel": "6e400003"})

        finish.set()
        await hass.async_block_till_done()
        # The frontend continues once the progress is done.
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["reason"] == "channels_detected"
    assert entry.data[CONF_COMMAND_CHAR] == C3
    assert entry.data[CONF_REPLY_CHAR] == C1
    reload.assert_called_once_with(entry.entry_id)


async def test_reconfigure_finishes_with_the_detected_channels(hass):
    entry = make_entry(hass)
    result = await start_reconfigure(hass, entry)
    with patch(DETECT, AsyncMock(return_value=(C3, C1))), \
         patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result, _ = await finish_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "channels_detected"
    assert not result.get("description_placeholders")


async def test_reconfigure_stores_whether_it_keeps_time(hass):
    entry = make_entry(hass)

    async def detect(self, **kwargs):
        self.detected_clock = False
        return C4, C1

    result = await start_reconfigure(hass, entry)
    with patch(DETECT, detect), patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result, _ = await finish_progress(hass, result)
    assert result["reason"] == "channels_detected"
    assert entry.data["has_clock"] is False


async def test_reconfigure_without_reply_changes_nothing(hass):
    entry = make_entry(hass)
    result = await start_reconfigure(hass, entry)
    with patch(DETECT, AsyncMock(return_value=None)), \
         patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result, _ = await finish_progress(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "no_reply"}
    assert entry.data[CONF_COMMAND_CHAR] == C4
    reload.assert_not_called()


async def test_reconfigure_form_is_plain(hass):
    entry = make_entry(hass, None, None)
    result = await start_reconfigure(hass, entry)
    assert not result.get("description_placeholders")
    assert result["errors"] == {}


async def test_reconfigure_uses_the_loaded_instance(hass):
    """The device takes a single connection, so probe through the running one."""
    from custom_components.godrej_aer.godrej.godrej import SmartMatic

    entry = make_entry(hass)
    running = SmartMatic(hass, MAC, command_char=C4, reply_char=C1)
    entry.runtime_data = SimpleNamespace(instance=running)
    seen = []

    async def detect(self, **kwargs):
        seen.append(self)
        return (C4, C1)

    result = await start_reconfigure(hass, entry)
    with patch.object(SmartMatic, "detect_channels", detect), \
         patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await finish_progress(hass, result)
    assert seen == [running]


DISABLED_BY_DEFAULT = (
    ("sensor", "command_channel"), ("sensor", "reply_channel"), ("sensor", "battery_voltage"),
)


def enable_diagnostics(hass, entry):
    """Pre-register the entities that are disabled by default, enabled."""
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    for platform, key in DISABLED_BY_DEFAULT:
        ent_reg.async_get_or_create(
            platform, DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry,
            suggested_object_id={
                "command_channel": "hall_channel_command",
                "reply_channel": "hall_channel_reply",
                "battery_voltage": "hall_battery_voltage",
            }[key],
            disabled_by=None,
        )


BT_COORD = "homeassistant.components.bluetooth.update_coordinator"
ADVERTS: list = []  # the coordinator's advertisement callback, per setup


async def setup_entry(hass, entry, service_info=None, connect=None):
    from custom_components.godrej_aer.godrej.godrej import SmartMatic

    patches = [
        patch("homeassistant.components.bluetooth.async_register_callback", return_value=lambda: None),
        # Where the Bluetooth coordinator looks them up.
        patch(f"{BT_COORD}.async_address_present", return_value=True),
        patch(f"{BT_COORD}.async_register_callback",
              side_effect=lambda hass, cb, matcher, mode: ADVERTS.append(cb) or (lambda: None)),
        patch(f"{BT_COORD}.async_track_unavailable",
              side_effect=lambda hass, cb, mac, connectable=True: UNAVAILABLE.append(cb) or (lambda: None)),
        patch("homeassistant.components.bluetooth.async_last_service_info", return_value=service_info),
        patch("homeassistant.components.bluetooth.async_scanner_by_source",
              return_value=SimpleNamespace(name="esp32-bluetooth-proxy")),
        patch.object(SmartMatic, "connect_if_needed", connect or AsyncMock(return_value=True)),
        patch.object(SmartMatic, "disconnect", AsyncMock()),
    ]
    for p in patches:
        p.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        for p in patches:
            p.stop()


async def test_entry_setup_uses_saved_channels_and_shows_diagnostics(hass):
    entry = make_entry(hass)
    enable_diagnostics(hass, entry)
    info = SimpleNamespace(rssi=-61, source="80:F3:DA:41:58:DB")
    await setup_entry(hass, entry, info)

    instance = entry.runtime_data.instance
    assert (instance.command_char, instance.reply_char) == (C4, C1)

    command = hass.states.get("sensor.hall_channel_command")
    reply = hass.states.get("sensor.hall_channel_reply")
    signal = hass.states.get("sensor.hall_signal_strength")
    assert command.attributes["friendly_name"] == "Hall Channel: command"
    assert command.state == "6e400004" and command.attributes["uuid"] == C4
    assert reply.state == "6e400001"
    assert signal.state == "-61"
    assert signal.attributes["source"] == "80:F3:DA:41:58:DB"
    assert signal.attributes["source_name"] == "esp32-bluetooth-proxy"
    assert signal.attributes["unit_of_measurement"] == "dBm"


async def test_legacy_entry_uses_the_default_channels(hass):
    entry = make_entry(hass, None, None)
    enable_diagnostics(hass, entry)
    await setup_entry(hass, entry)
    assert hass.states.get("sensor.hall_channel_command").state == "6e400003"
    assert hass.states.get("sensor.hall_channel_reply").state == "6e400001"
    assert hass.states.get("sensor.hall_signal_strength").state == "unknown"


async def test_diagnostic_sensors_are_disabled_by_default(hass):
    from homeassistant.helpers import entity_registry as er

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    ent_reg = er.async_get(hass)
    for platform, key in DISABLED_BY_DEFAULT:
        entity_id = ent_reg.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        assert ent_reg.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None
    assert hass.states.get("sensor.hall_battery") is not None
    assert hass.states.get("sensor.hall_signal_strength") is not None


async def call(hass, domain, service, entity_id, **data):
    await hass.services.async_call(
        domain, service, {"entity_id": entity_id, **data}, blocking=True
    )


async def expect_unconfirmed(hass, domain, service, entity_id, key="command_not_confirmed", **data):
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError) as err:
        await call(hass, domain, service, entity_id, **data)
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == key


@pytest.mark.parametrize("entity_id, method", [
    ("button.hall_spray_now", "spray_now"),
    ("button.hall_reset_refill_level", "reset_refill"),
])
async def test_unconfirmed_button_press_shows_an_error(hass, entity_id, method):
    from custom_components.godrej_aer.godrej.exception import CommandNotConfirmed

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance

    with patch.object(instance, method, AsyncMock(side_effect=CommandNotConfirmed("x"))):
        await expect_unconfirmed(hass, "button", "press", entity_id)
    with patch.object(instance, method, AsyncMock()) as ok:
        await call(hass, "button", "press", entity_id)
    ok.assert_awaited_once()


async def test_auto_spray_mode_follows_the_acknowledgement(hass):
    from custom_components.godrej_aer.godrej.exception import CommandNotConfirmed

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    mode = "select.hall_spray_auto"
    assert hass.states.get(mode).attributes["options"] == [
        "Off", "Every 10 min", "Every 20 min", "Every 40 min",
    ]
    before = hass.states.get(mode).state

    # Not acknowledged, or unreachable: unchanged (the dropdown reverts).
    with patch.object(instance, "set_spray_interval", AsyncMock(side_effect=CommandNotConfirmed("x"))):
        await expect_unconfirmed(hass, "select", "select_option", mode, option="Every 20 min")
    assert hass.states.get(mode).state == before
    with patch.object(instance, "set_spray_interval", AsyncMock(side_effect=DeviceConnectionError("x"))):
        await expect_unconfirmed(
            hass, "select", "select_option", mode, key="cannot_reach", option="Every 20 min"
        )
    assert hass.states.get(mode).state == before

    # Acknowledged: shown straight away, before the status read-back.
    with patch.object(instance, "set_spray_interval", AsyncMock()) as interval:
        await call(hass, "select", "select_option", mode, option="Every 20 min")
    interval.assert_awaited_once_with(20)
    assert hass.states.get(mode).state == "Every 20 min"

    with patch.object(instance, "stop_spray", AsyncMock(side_effect=CommandNotConfirmed("x"))):
        await expect_unconfirmed(hass, "select", "select_option", mode, option="Off")
    assert hass.states.get(mode).state == "Every 20 min"
    with patch.object(instance, "stop_spray", AsyncMock()) as off:
        await call(hass, "select", "select_option", mode, option="Off")
        await call(hass, "select", "select_option", mode, option="Off")
    assert off.await_count == 2
    assert hass.states.get(mode).state == "Off"


async def test_auto_spray_mode_shows_the_device_state(hass):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    from test_devicestatus_frame import FRAME
    deliver_status(instance, FRAME)
    await hass.async_block_till_done()
    state = hass.states.get("select.hall_spray_auto")
    assert state.state == "Every 40 min"
    assert state.attributes["started_by"] == "app"  # cS 114

    # The running interval again: nothing sent (it would restart the cycle
    # with a spray). Off is always sent.
    with patch.object(instance, "set_spray_interval", AsyncMock()) as interval, \
         patch.object(instance, "stop_spray", AsyncMock()) as off:
        await call(hass, "select", "select_option", "select.hall_spray_auto", option="Every 40 min")
        await call(hass, "select", "select_option", "select.hall_spray_auto", option="Off")
        await call(hass, "select", "select_option", "select.hall_spray_auto", option="Off")
    interval.assert_not_awaited()
    assert off.await_count == 2


async def test_identify(hass):
    from custom_components.godrej_aer.godrej.exception import NotIdle

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    with patch.object(instance, "identify", AsyncMock(side_effect=NotIdle("x"))):
        await expect_unconfirmed(hass, "button", "press", "button.hall_identify", key="auto_running")
    with patch.object(instance, "identify", AsyncMock(side_effect=NotIdle("x", schedule=True))):
        await expect_unconfirmed(hass, "button", "press", "button.hall_identify", key="schedule_running")
    with patch.object(instance, "identify", AsyncMock()) as ok:
        await call(hass, "button", "press", "button.hall_identify")
    ok.assert_awaited_once()
    assert hass.states.get("button.hall_identify").attributes["device_class"] == "identify"


async def test_old_auto_spray_entities_are_removed(hass):
    from homeassistant.helpers import entity_registry as er

    entry = make_entry(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("switch", DOMAIN, f"{entry.entry_id}_auto_spray", config_entry=entry)
    registry.async_get_or_create("select", DOMAIN, f"{entry.entry_id}_spray_interval", config_entry=entry)
    await setup_entry(hass, entry)
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_auto_spray") is None
    assert registry.async_get_entity_id("select", DOMAIN, f"{entry.entry_id}_spray_interval") is None


async def test_signal_strength_updates_are_throttled(hass):
    from custom_components.godrej_aer.sensor import SmartMaticSignalStrengthSensor

    entry = make_entry(hass)
    await setup_entry(hass, entry, SimpleNamespace(rssi=-61, source="proxy"))
    sensor = next(
        e for e in hass.data["entity_components"]["sensor"].entities
        if isinstance(e, SmartMaticSignalStrengthSensor)
    )
    with patch("homeassistant.components.bluetooth.async_scanner_by_source", return_value=None):
        sensor._on_advertisement(SimpleNamespace(rssi=-63, source="proxy"), None)
        assert hass.states.get("sensor.hall_signal_strength").state == "-61"  # small change
        sensor._on_advertisement(SimpleNamespace(rssi=-70, source="proxy"), None)
        assert hass.states.get("sensor.hall_signal_strength").state == "-70"  # big change
        sensor._on_advertisement(SimpleNamespace(rssi=-71, source="other"), None)
        assert hass.states.get("sensor.hall_signal_strength").state == "-71"  # new source


async def test_health_entities(hass):
    from homeassistant.helpers import entity_registry as er

    UNAVAILABLE.clear()
    entry = make_entry(hass)
    ent_reg = er.async_get(hass)
    await setup_entry(hass, entry)

    instance = entry.runtime_data.instance
    status_id = "binary_sensor.hall_connection_status"
    status = hass.states.get(status_id)
    assert status.state == "unknown"
    assert status.attributes["friendly_name"] == "Hall Connection status"
    assert ent_reg.async_get(status_id).translation_key == "status"

    # A status reply arrives: OK, with the time as an attribute.
    from test_devicestatus_frame import FRAME
    instance.schedules = []  # known: no startup schedule read in this test
    deliver_status(instance, FRAME)
    await hass.async_block_till_done()
    status = hass.states.get(status_id)
    assert status.state == "on"
    assert status.attributes["last_update"] is not None

    # The routine disconnect after an exchange changes nothing.
    instance._on_disconnect(None)
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == "on"

    # No advertisements for a while: Unreachable, until it is seen again.
    assert len(UNAVAILABLE) == 1
    UNAVAILABLE[0](SimpleNamespace(address=MAC, time=0.0, name="Smart Matic"))
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == "off"
    instance.set_advertising(True)
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == "on"

    # The raw link sensor is a new entity, disabled by default.
    link = ent_reg.async_get("binary_sensor.hall_connection_link")
    assert link.unique_id == f"{entry.entry_id}_bluetooth_link"
    assert link.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert link.translation_key == "bluetooth_link"
    assert hass.states.get("binary_sensor.hall_connection_link") is None


async def test_reset_refill_is_in_the_configuration_section(hass):
    from homeassistant.helpers import entity_registry as er

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    ent_reg = er.async_get(hass)
    reset = next(
        e for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.unique_id == f"{entry.entry_id}_reset_refill"
    )
    spray = next(
        e for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.unique_id == f"{entry.entry_id}_trigger"
    )
    assert reset.entity_category is er.EntityCategory.CONFIG
    assert spray.entity_category is None


async def frontend_like_run(hass, flow_id, start):
    """Drive a flow the way the frontend does: refresh (GET = configure) on
    every change and progress event. Returns the last result, and whether
    the frontend would have hit "Invalid flow specified"."""
    from homeassistant.data_entry_flow import (
        EVENT_DATA_ENTRY_FLOW_PROGRESSED,
        EVENT_DATA_ENTRY_FLOW_PROGRESS_UPDATE,
        UnknownFlow,
    )

    results, invalid = [], []

    async def refresh(event):
        if event.data.get("flow_id") != flow_id:
            return
        await asyncio.sleep(0.01)  # the browser's round trip
        try:
            results.append(await hass.config_entries.flow.async_configure(flow_id))
        except UnknownFlow:
            invalid.append(event.event_type)

    unsub = [
        hass.bus.async_listen(EVENT_DATA_ENTRY_FLOW_PROGRESSED, refresh),
        hass.bus.async_listen(EVENT_DATA_ENTRY_FLOW_PROGRESS_UPDATE, refresh),
    ]
    results.append(await start())
    for _ in range(100):
        await asyncio.sleep(0.01)
    for u in unsub:
        u()
    return results[-1], invalid


async def staged_detect(self, *, progress=None, **kwargs):
    progress("connecting", {}, 0.0)
    await asyncio.sleep(0.05)
    for i, ch in enumerate(("6e400002", "6e400003", "6e400004"), 1):
        progress("probing", {"index": str(i), "total": "3", "channel": ch}, (i - 1) / 6)
        await asyncio.sleep(0.05)
    return (C4, C1)


async def test_reconfigure_with_frontend_refreshes(hass):
    entry = make_entry(hass, C3, C1)
    result = await start_reconfigure(hass, entry)
    flow_id = result["flow_id"]
    with patch(DETECT, staged_detect), patch.object(hass.config_entries, "async_schedule_reload"):
        last, invalid = await frontend_like_run(
            hass, flow_id, lambda: hass.config_entries.flow.async_configure(flow_id, {})
        )
    assert invalid == []
    assert last["type"] is FlowResultType.ABORT and last["reason"] == "channels_detected"
    assert entry.data[CONF_COMMAND_CHAR] == C4


async def test_setup_with_frontend_refreshes(hass):
    result = await start_flow(hass)
    flow_id = result["flow_id"]
    with patch(DETECT, staged_detect), no_setup(), \
         patch(f"{CF}.SmartMatic.disconnect", AsyncMock()), \
         patch(f"{CF}.async_discovered_service_info", return_value=discovered()):
        last, invalid = await frontend_like_run(
            hass, flow_id, lambda: hass.config_entries.flow.async_configure(flow_id, {"mac": MAC})
        )
    assert invalid == []
    assert last["type"] is FlowResultType.CREATE_ENTRY


async def test_battery_is_shown_as_a_percentage(hass):
    from homeassistant.helpers import entity_registry as er
    from test_devicestatus_frame import FRAME

    entry = make_entry(hass)
    enable_diagnostics(hass, entry)
    await setup_entry(hass, entry)
    deliver_status(entry.runtime_data.instance, FRAME)
    await hass.async_block_till_done()

    battery = hass.states.get("sensor.hall_battery")
    voltage = hass.states.get("sensor.hall_battery_voltage")
    mv = entry.runtime_data.instance.device_status.battery_mv
    assert battery.attributes["friendly_name"] == "Hall Battery"
    assert voltage.attributes["friendly_name"] == "Hall Battery voltage"
    assert battery.attributes["device_class"] == "battery"
    assert battery.attributes["unit_of_measurement"] == "%"
    assert battery.state == str(entry.runtime_data.instance.device_status.battery_percent)
    assert voltage.state == str(mv)
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get("sensor.hall_battery_voltage").entity_category is er.EntityCategory.DIAGNOSTIC


async def test_flat_batteries_show_a_toast_but_keep_the_new_state(hass):
    from custom_components.godrej_aer.godrej.exception import BatteryTooLow

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    flat = AsyncMock(side_effect=BatteryTooLow("x"))

    with patch.object(instance, "spray_now", flat):
        await expect_unconfirmed(hass, "button", "press", "button.hall_spray_now", key="battery_too_low")
    with patch.object(instance, "set_spray_interval", flat):
        await expect_unconfirmed(
            hass, "select", "select_option", "select.hall_spray_auto",
            key="battery_too_low", option="Every 40 min",
        )
    # The device did take the mode change; it just can't spray.
    assert hass.states.get("select.hall_spray_auto").state == "Every 40 min"


async def test_control_names(hass):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    names = {
        "button.hall_spray_now": "Hall Spray now",
        "button.hall_reset_refill_level": "Hall Reset refill level",
        "select.hall_spray_auto": "Hall Spray: auto",
        "sensor.hall_signal_strength": "Hall Signal strength",
    }
    for entity_id, name in names.items():
        assert hass.states.get(entity_id).attributes["friendly_name"] == name


async def test_reads_follow_the_advertisements(hass):
    """The coordinator reads the device when an advertisement arrives and
    the device code says a read is due (it applies the hourly read and
    the back-off); nothing is read before the device is heard."""
    import time
    from datetime import timedelta
    from homeassistant.components.bluetooth import BluetoothChange
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    from custom_components.godrej_aer.godrej.godrej import SmartMatic

    entry = make_entry(hass)
    connect = AsyncMock(return_value=True)
    due = [True]
    with patch.object(SmartMatic, "needs_status_read", lambda self: due[0]):
        await setup_entry(hass, entry, connect=connect)
        with patch.object(SmartMatic, "connect_if_needed", connect):
            assert connect.await_count == 0
            heard = SimpleNamespace(address=MAC, name="Smart Matic", rssi=-70, time=time.monotonic())
            ADVERTS[-1](heard, BluetoothChange.ADVERTISEMENT)
            await hass.async_block_till_done()
            assert connect.await_count == 1
            assert entry.runtime_data.instance.heard

            due[0] = False
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
            ADVERTS[-1](heard, BluetoothChange.ADVERTISEMENT)
            await hass.async_block_till_done()
            assert connect.await_count == 1  # not due: nothing read


# --- schedules ---------------------------------------------------------------

def _sched(days, start, end, interval):
    from datetime import time as dtime
    from custom_components.godrej_aer.godrej.schedule import Schedule

    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    return Schedule.create(days, dtime(h1, m1), dtime(h2, m2), interval)


async def test_schedules_sensor_lists_what_the_device_holds(hass):
    from custom_components.godrej_aer.godrej.events import DEVICE_SCHEDULES_UPDATE

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    assert hass.states.get("sensor.hall_schedules").state == "unknown"

    instance.schedules = [_sched("daily", "08:00", "22:30", 40),
                          _sched(["sat", "sun"], "10:00", "12:00", 20)]
    instance.eventbus.send(DEVICE_SCHEDULES_UPDATE, instance)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.hall_schedules")
    assert state.state == "2"
    assert state.attributes["friendly_name"] == "Hall Schedules"
    assert state.attributes["schedules"] == [
        {"number": 1, "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
         "start": "08:00", "end": "22:00", "next_day": False, "interval": 40},
        {"number": 2, "days": ["Sat", "Sun"], "start": "10:00", "end": "12:00", "next_day": False,
         "interval": 20},
    ]


async def test_schedules_are_read_once_after_the_first_status(hass):
    from custom_components.godrej_aer.godrej.godrej import SmartMatic
    from test_devicestatus_frame import FRAME

    entry = make_entry(hass)
    with patch.object(SmartMatic, "read_schedules", AsyncMock(return_value=[])) as read:
        await setup_entry(hass, entry)
        instance = entry.runtime_data.instance
        read.assert_not_awaited()
        deliver_status(instance, FRAME)
        await hass.async_block_till_done()
        read.assert_awaited_once()
        instance.schedules = []
        deliver_status(instance, FRAME)
        await hass.async_block_till_done()
        read.assert_awaited_once()


async def test_schedule_actions(hass):
    from homeassistant.exceptions import ServiceValidationError

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance

    with patch.object(instance, "add_schedule", AsyncMock(return_value=[])) as add:
        await hass.services.async_call(DOMAIN, "add_schedule", {
            "days": ["mon", "fri"], "start": "07:00", "end": "14:00", "interval": "10",
        }, blocking=True)
    add.assert_awaited_once_with(_sched(["mon", "fri"], "07:00", "14:00", 10))

    with patch.object(instance, "set_schedules", AsyncMock(return_value=[])) as set_:
        await hass.services.async_call(DOMAIN, "set_schedules", {"schedules": [
            {"days": "daily", "start": "08:00", "end": "22:30", "interval": 40},
            {"days": ["sat"], "start": "06:00", "end": "07:00", "interval": 20},
        ]}, blocking=True)
        set_.assert_awaited_once_with([
            _sched("daily", "08:00", "22:30", 40), _sched(["sat"], "06:00", "07:00", 20),
        ], None)
        set_.reset_mock()
        await hass.services.async_call(DOMAIN, "clear_schedules", {}, blocking=True)
        set_.assert_awaited_once_with([])

    with patch.object(instance, "remove_schedule", AsyncMock(return_value=[])) as remove:
        await hass.services.async_call(DOMAIN, "remove_schedule", {"number": 2}, blocking=True)
    remove.assert_awaited_once_with(2)

    # Impossible schedules are refused before anything is sent.
    with pytest.raises(ServiceValidationError, match="shorter than one"):
        await hass.services.async_call(DOMAIN, "add_schedule", {
            "days": ["mon"], "start": "14:00", "end": "14:05", "interval": "10",
        }, blocking=True)


async def test_schedule_action_reports_device_failures(hass):
    from homeassistant.exceptions import HomeAssistantError
    from custom_components.godrej_aer.godrej.exception import CommandNotConfirmed

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    with patch.object(instance, "set_schedules", AsyncMock(side_effect=CommandNotConfirmed("x"))):
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(DOMAIN, "clear_schedules", {}, blocking=True)
    assert err.value.translation_key == "command_not_confirmed"


async def test_update_status_also_reads_schedules(hass):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    with patch.object(instance, "connect", AsyncMock(return_value=True)) as connect, \
         patch.object(instance, "read_schedules", AsyncMock(return_value=[])) as read:
        await hass.services.async_call(DOMAIN, "update_status", {}, blocking=True)
    connect.assert_awaited_once()
    read.assert_awaited_once()


async def test_actions_use_the_current_target_helper(hass, caplog):
    """HA 2026.10 removed the old (hass, call) form; it used to log a warning."""
    from homeassistant.helpers import device_registry as dr

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    device = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0]
    instance = entry.runtime_data.instance
    with patch.object(instance, "connect", AsyncMock(return_value=True)) as connect, \
         patch.object(instance, "read_schedules", AsyncMock(return_value=[])):
        await hass.services.async_call(
            DOMAIN, "update_status", {"device_id": device.id}, blocking=True
        )
    connect.assert_awaited_once_with(purpose="update status")
    assert "deprecated argument hass" not in caplog.text


async def test_update_status_button(hass):
    from homeassistant.helpers import entity_registry as er

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    button = "button.hall_update_status"
    assert er.async_get(hass).async_get(button).entity_category is er.EntityCategory.DIAGNOSTIC
    with patch.object(instance, "connect", AsyncMock(return_value=True)) as connect, \
         patch.object(instance, "read_schedules", AsyncMock(return_value=[])) as read:
        await call(hass, "button", "press", button)
    connect.assert_awaited_once()
    read.assert_awaited_once()
    from homeassistant.exceptions import HomeAssistantError

    with patch.object(instance, "connect", AsyncMock(side_effect=DeviceConnectionError("x"))):
        with pytest.raises(HomeAssistantError) as err:
            await call(hass, "button", "press", button)
    # The same error as the action, naming the device.
    assert err.value.translation_key == "update_failed"
    assert err.value.translation_placeholders == {"names": "Hall"}


async def test_updated_last_sensor(hass):
    from homeassistant.helpers import entity_registry as er
    from test_devicestatus_frame import FRAME

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    instance.schedules = []  # no startup schedule read in this test
    sensor = "sensor.hall_updated_last"
    assert hass.states.get(sensor).state == "unknown"
    assert er.async_get(hass).async_get(sensor).entity_category is er.EntityCategory.DIAGNOSTIC
    deliver_status(instance, FRAME)
    await hass.async_block_till_done()
    state = hass.states.get(sensor)
    assert state.attributes["device_class"] == "timestamp"
    from homeassistant.util import dt as dt_util
    assert dt_util.parse_datetime(state.state) == instance.last_update.replace(microsecond=0)


async def test_stop_schedule_action(hass):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    with patch.object(entry.runtime_data.instance, "stop_schedule", AsyncMock()) as stop:
        await hass.services.async_call(DOMAIN, "stop_schedule", {"schedule": "2"}, blocking=True)
    stop.assert_awaited_once_with(2)


async def test_status_combines_mode_and_reachability(hass):
    from test_devicestatus_frame import FRAME

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    sensor = "sensor.hall_status"
    state = hass.states.get(sensor)
    assert state.attributes["options"] == ["idle", "auto", "schedule", "disconnected"]

    instance.set_advertising(True)  # heard
    with patch.object(instance, "read_schedules", AsyncMock()):  # read after the first status
        deliver_status(instance, FRAME)  # cS 114: auto, started from an app or HA
        await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "auto"

    instance.set_advertising(False)  # HA reports it gone
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "disconnected"

    instance.set_advertising(True)
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "auto"



async def test_set_schedules_refuses_when_the_device_list_changed(hass):
    from custom_components.godrej_aer.godrej.exception import SchedulesChanged

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    expected = [{"days": ["Mon"], "start": "08:00", "end": "10:00", "interval": 20}]
    from homeassistant.exceptions import HomeAssistantError

    with patch.object(instance, "set_schedules", AsyncMock(side_effect=SchedulesChanged("x"))) as set_:
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(DOMAIN, "set_schedules", {
                "schedules": [{"days": ["mon"], "start": "08:00", "end": "11:00", "interval": 20}],
                "expected": expected,
            }, blocking=True)
    assert err.value.translation_key == "schedules_changed"
    assert set_.await_args.args[1] == expected


async def test_auto_mode_is_refused_during_a_schedule_run(hass):
    from custom_components.godrej_aer.godrej.devicestatus import DeviceStatus
    from custom_components.godrej_aer.godrej.godrej import encode_message

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    instance.device_status = DeviceStatus(encode_message(mT=157, mN=157, ack=200, bV=3100, cS=121))
    with patch.object(instance, "stop_spray", AsyncMock()) as off:
        await expect_unconfirmed(
            hass, "select", "select_option", "select.hall_spray_auto",
            key="auto_during_schedule", option="Off",
        )
    off.assert_not_awaited()


async def test_schedules_are_restored_after_a_restart(hass):
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    listed = [{"number": 1, "days": ["Mon"], "start": "08:00", "end": "10:00", "interval": 20}]
    mock_restore_cache(hass, [State(
        "sensor.hall_schedules", "1",
        {"schedules": listed, "running": 1, "last_read": "2026-11-01T08:00:00+00:00"},
    )])
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    state = hass.states.get("sensor.hall_schedules")
    assert state.state == "1"
    assert state.attributes["schedules"] == listed
    assert state.attributes["running"] is None  # not known until read again
    assert state.attributes["last_read"] == "2026-11-01T08:00:00+00:00"


async def test_diagnostics_report(hass):
    from custom_components.godrej_aer.diagnostics import async_get_config_entry_diagnostics
    from test_devicestatus_frame import FRAME

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    deliver_status(entry.runtime_data.instance, FRAME)
    await hass.async_block_till_done()
    with patch("homeassistant.components.bluetooth.async_last_service_info", return_value=None):
        report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["entry"]["data"]["mac"] == "**REDACTED**"
    assert report["entry"]["data"]["name"] == "**REDACTED**"
    assert report["entry"]["title"] == "**REDACTED**"
    assert MAC not in str(report) and MAC.lower() not in str(report)
    assert report["channels"] == {"command": C4, "reply": C1}
    assert report["status"]["mode_code"] == 114 and report["status"]["spray_interval"] == 40
    assert report["status"]["battery_mv"] and report["status"]["raw"]
    assert set(report["connection"]) >= {"reachable", "advertising", "last_exchange_ok", "poll_failures"}
    assert report["schedules"] is None  # not read yet
    assert set(report["clock"]) == {
        "has_clock", "batteries_replaced_at", "device_time", "offset_seconds", "read_at",
    }
    assert report["clock"]["has_clock"] is True

    from custom_components.godrej_aer.godrej.schedule import Schedule
    from datetime import time
    instance = entry.runtime_data.instance
    instance.schedules = [Schedule.create(["mon"], time(8), time(10), 20)]
    with patch("homeassistant.components.bluetooth.async_last_service_info", return_value=None):
        report = await async_get_config_entry_diagnostics(hass, entry)
    listed = report["schedules"]["list"][0]
    assert (listed["start"], listed["end"], listed["sprays"]) == ("08:00", "10:00", 6)
    assert listed["raw"] and report["schedules"]["running"] is None


async def test_deleting_a_device_offers_it_again(hass):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    with patch("custom_components.godrej_aer.bluetooth.async_rediscover_address") as again:
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    again.assert_called_once_with(hass, MAC)


async def test_actions_stay_registered_without_loaded_devices(hass):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, "update_status")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # Registered once at integration load, not per device (action-setup).
    assert hass.services.has_service(DOMAIN, "update_status")


async def test_device_entities_are_unavailable_while_not_heard(hass, caplog):
    from test_devicestatus_frame import FRAME

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    deliver_status(instance, FRAME)
    await hass.async_block_till_done()

    readings = ["sensor.hall_battery", "button.hall_spray_now", "button.hall_reset_refill_level",
                "button.hall_identify", "select.hall_spray_auto", "sensor.hall_schedules",
                "sensor.hall_signal_strength", "button.hall_update_status"]
    always = ["sensor.hall_status", "binary_sensor.hall_connection_status",
              "sensor.hall_updated_last"]
    for entity_id in readings + always:
        assert hass.states.get(entity_id).state != "unavailable", entity_id

    caplog.clear()
    instance.set_advertising(False)
    instance.set_advertising(False)
    await hass.async_block_till_done()
    for entity_id in readings:
        assert hass.states.get(entity_id).state == "unavailable", entity_id
    for entity_id in always:
        assert hass.states.get(entity_id).state != "unavailable", entity_id
    assert caplog.text.count("no longer heard") == 1

    instance.set_advertising(True)
    instance.set_advertising(True)
    await hass.async_block_till_done()
    for entity_id in readings:
        assert hass.states.get(entity_id).state != "unavailable", entity_id
    assert caplog.text.count("heard again") == 1


def advert(name="Smart Matic"):
    return SimpleNamespace(address=MAC, name=name, rssi=-70)


async def test_bluetooth_discovery_sets_up_after_confirming(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=advert()
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "bluetooth_confirm"
    # Every placeholder the step's title and text use must be given.
    assert result["description_placeholders"] == {"name": "Smart Matic", "address": MAC}
    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"]["title_placeholders"] == {"name": "Smart Matic"}

    detect = AsyncMock(side_effect=[None, (C4, C1)])
    with patch(DETECT, detect), no_setup(), patch(f"{CF}.SmartMatic.disconnect", AsyncMock()):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result, _ = await finish_progress(hass, result)
        # No reply: back to the confirmation, to try again.
        assert result["step_id"] == "bluetooth_confirm"
        assert result["errors"] == {"base": "no_reply"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result, _ = await finish_progress(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Smart Matic"
    assert result["result"].unique_id == MAC.lower()


async def test_bluetooth_discovery_of_a_configured_or_other_device_aborts(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=advert("Other")
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_supported"

    make_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=advert()
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_manual_mac_setup(hass):
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mac": "manual_mac"})
    assert result["step_id"] == "manual_mac"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mac": "not-a-mac"})
    assert result["errors"] == {"base": "invalid_mac"}

    detect = AsyncMock(side_effect=[RuntimeError("boom"), (C4, C1)])
    with patch(DETECT, detect), no_setup(), patch(f"{CF}.SmartMatic.disconnect", AsyncMock()):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mac": MAC})
        result, _ = await finish_progress(hass, result)
        # An unexpected error: back to the address form, to try again.
        assert result["step_id"] == "manual_mac" and result["errors"] == {"base": "unknown"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mac": MAC})
        result, _ = await finish_progress(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_configured_devices_are_not_listed(hass):
    make_entry(hass)
    other = SimpleNamespace(address="11:22:33:44:55:66", name="Phone")
    with patch(f"{CF}.async_discovered_service_info",
               return_value=[*discovered(), other]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    schema = result["data_schema"].schema
    options = next(iter(schema.values())).container
    assert list(options) == ["manual_mac"]


async def test_update_status_action_errors(hass):
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    with patch.object(instance, "connect", AsyncMock(side_effect=DeviceConnectionError("gone"))):
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(DOMAIN, "update_status", {}, blocking=True)
    assert err.value.translation_key == "update_failed"
    assert err.value.translation_placeholders == {"names": "Hall"}  # not the address

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "update_status", {}, blocking=True)
    assert err.value.translation_key == "no_devices_matched"


async def test_update_entity_reads_the_device_now(hass, caplog):
    """update_entity on any of its entities reads the status and schedules,
    as Update status does; a failure is logged briefly, not raised."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "homeassistant", {})
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance

    async def slow_read(**_kwargs):
        await asyncio.sleep(0.05)  # a read takes a while: the second joins it
        return True

    with patch.object(instance, "connect", AsyncMock(side_effect=slow_read)) as connect, \
         patch.object(instance, "read_schedules", AsyncMock(return_value=[])) as read:
        await hass.services.async_call(
            "homeassistant", "update_entity",
            {"entity_id": ["sensor.hall_schedules", "sensor.hall_battery"]}, blocking=True,
        )
    assert connect.await_count == 1  # both entities share one read
    read.assert_awaited_once()

    caplog.clear()
    with patch.object(instance, "connect", AsyncMock(side_effect=DeviceConnectionError("gone"))):
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": "sensor.hall_schedules"}, blocking=True,
        )
    assert "Could not update" in caplog.text
    assert "Traceback" not in caplog.text


async def test_status_is_read_around_each_schedule_start_and_end(hass, freezer):
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    from custom_components.godrej_aer.godrej.events import DEVICE_SCHEDULES_UPDATE

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    instance.set_advertising(True)
    now = dt_util.now()
    start = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)
    end = start + timedelta(minutes=20)
    day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][start.weekday()]
    instance.schedules = [_sched([day], f"{start:%H:%M}", f"{end:%H:%M}", 20)]
    with patch.object(instance, "watch_schedule_change") as watch:
        instance.eventbus.send(DEVICE_SCHEDULES_UPDATE, instance)
        await hass.async_block_till_done()
        freezer.move_to(start + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        watch.assert_called_once_with(starting=True)
        freezer.move_to(end + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert watch.call_args_list[-1].kwargs == {"starting": False}



async def test_a_device_without_a_clock_has_no_schedules(hass):
    """No Schedules sensor (an old one is removed), and the schedule
    actions say why they can't be used."""
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er

    entry = make_entry(hass, has_clock=False)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_schedules", config_entry=entry,
        suggested_object_id="hall_schedules",
    )
    await setup_entry(hass, entry)
    assert hass.states.get("sensor.hall_schedules") is None
    assert er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_schedules") is None
    assert hass.states.get("sensor.hall_status").attributes["has_clock"] is False
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "add_schedule", {
            "days": ["mon"], "start": "07:00", "end": "14:00", "interval": "10",
        }, blocking=True)
    assert err.value.translation_key == "no_clock"



async def test_status_shows_whether_the_device_keeps_time(hass):
    entry = make_entry(hass)  # from before the check: taken to keep time
    await setup_entry(hass, entry)
    assert hass.states.get("sensor.hall_status").attributes["has_clock"] is True



async def test_a_press_that_waits_too_long_says_the_device_is_busy(hass):
    """It didn't get its turn within 5 s: not an unreachable device."""
    from homeassistant.exceptions import HomeAssistantError
    from custom_components.godrej_aer.godrej.exception import DeviceBusy

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    with patch.object(instance, "spray_now", AsyncMock(side_effect=DeviceBusy("busy"))):
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(
                "button", "press", {"entity_id": "button.hall_spray_now"}, blocking=True
            )
    assert err.value.translation_key == "device_busy"



def battery_frame(mv):
    from test_devicestatus_frame import FRAME

    at = FRAME.index(b"bbV\x1a") + 4
    return FRAME[:at] + mv.to_bytes(4, "big") + FRAME[at + 4:]


async def test_batteries_replaced_sensor(hass):
    """Diagnostic; unknown until new batteries are seen, then their time."""
    from homeassistant.const import EntityCategory
    from homeassistant.helpers import entity_registry as er

    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    entity = er.async_get(hass).async_get("sensor.hall_batteries_replaced")
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get("sensor.hall_batteries_replaced").state == "unknown"

    with patch.object(instance, "check_clock", AsyncMock()) as clock:
        deliver_status(instance, battery_frame(2650))  # about 68 %
        deliver_status(instance, battery_frame(3120))  # 100 %
        await hass.async_block_till_done()
    clock.assert_awaited_once()
    state = hass.states.get("sensor.hall_batteries_replaced")
    assert state.state not in ("unknown", "unavailable")


async def test_a_swap_while_home_assistant_was_down_is_seen(hass):
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

    mock_restore_cache_with_extra_data(hass, [(
        State("sensor.hall_batteries_replaced", "2026-09-01T10:00:00+00:00"), {"percent": 60},
    )])
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    instance = entry.runtime_data.instance
    assert hass.states.get("sensor.hall_batteries_replaced").state == "2026-09-01T10:00:00+00:00"
    assert instance.restored_battery_percent == 60
    with patch.object(instance, "check_clock", AsyncMock()):
        deliver_status(instance, battery_frame(3120))
        await hass.async_block_till_done()
    assert hass.states.get("sensor.hall_batteries_replaced").state != "2026-09-01T10:00:00+00:00"
