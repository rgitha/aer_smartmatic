"""Connection regressions, runnable without Home Assistant or BLE hardware."""
import asyncio
import importlib
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


def load_driver():
    package = ModuleType("smartmatic_test_driver")
    package.__path__ = [str(Path(__file__).resolve().parents[1] /
                            "custom_components/aer_smartmatic/godrej")]
    stubs = {"smartmatic_test_driver": package}
    for name in ("bleak", "bleak_retry_connector", "homeassistant",
                 "homeassistant.components", "homeassistant.components.bluetooth",
                 "homeassistant.util", "homeassistant.exceptions"):
        stubs[name] = ModuleType(name)
    stubs["bleak"].BleakClient = Mock
    stubs["bleak_retry_connector"].establish_connection = AsyncMock()
    stubs["homeassistant.components.bluetooth"].async_ble_device_from_address = Mock()
    stubs["homeassistant.components.bluetooth"].async_last_service_info = Mock(return_value=None)
    stubs["homeassistant.components.bluetooth"].async_scanner_by_source = Mock(return_value=None)
    stubs["homeassistant.util"].dt = SimpleNamespace()
    stubs["homeassistant.exceptions"].HomeAssistantError = type(
        "HomeAssistantError", (Exception,), {})
    with patch.dict(sys.modules, stubs):
        return importlib.import_module("smartmatic_test_driver.godrej")


driver = load_driver()


def plain(payload):
    """``payload`` with the reply type as its mN, as the constants hold it
    (each message goes out with a number of its own)."""
    message = driver.cbor.loads(bytes(payload))
    return driver.with_mn(bytes(payload), message["mT"] + 50)


def sent(write):
    """The (characteristic, message) pairs written, as the constants hold them."""
    return [(call.args[0], plain(call.args[1])) for call in write.await_args_list]


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = driver.SmartMatic(None, "AA:BB:CC:DD:EE:FF")
        self.client = SimpleNamespace(is_connected=True, write_gatt_char=AsyncMock(),
                                      disconnect=AsyncMock())
        self.device.client = self.client
        self.device._subscribed = True
        # Clock checks are covered elsewhere: count it as just checked.
        self.device._clock_check_wanted = False
        self.device._clock_checked_at = __import__("time").monotonic()
        self.client.write_gatt_char.side_effect = self.confirm_write
        # These tests isolate link ownership; read-back is covered separately.
        self.refresh_patch = patch.object(self.device, "_schedule_refresh", Mock())
        self.refresh_patch.start()
        self.addCleanup(self.refresh_patch.stop)

    async def confirm_write(self, char, payload):
        message = driver.cbor.loads(payload)
        message_type = message["mT"]
        if message_type in (104, 105, 106):
            # The device copies the request's number (mN) into its reply.
            self.device._handle_message(driver.encode_message(
                mT=message_type + 50, mN=message["mN"], ack=200))

    async def asyncTearDown(self):
        tasks = [task for task in (self.device._disconnect_task, self.device._refresh_task)
                 if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_missing_client_is_connection_error(self):
        self.device.client = None
        with self.assertRaises(driver.ConnectionError):
            await self.device._write_message(driver.SPRAY_NOW_MSG)

    async def test_disconnect_during_notification_setup(self):
        self.device.client = None
        self.client.services = SimpleNamespace(get_service=Mock(return_value=object()))

        async def subscribe(*args):
            self.client.is_connected = False
            self.device._on_disconnect(self.client)

        with patch.object(driver, "establish_connection", AsyncMock(return_value=self.client)), \
             patch.object(driver.asyncio, "sleep", AsyncMock()), \
             patch.object(self.device, "_subscribe_notifications", subscribe):
            with self.assertRaises(driver.ConnectionError):
                await self.device.spray_now()
        self.client.write_gatt_char.assert_not_awaited()

    async def test_old_disconnect_does_not_clear_new_client(self):
        self.device._on_disconnect(SimpleNamespace(is_connected=False))
        self.assertIs(self.device.client, self.client)
        await self.device.spray_now()
        self.assertEqual(sent(self.client.write_gatt_char),
                         [(driver.DEFAULT_COMMAND_CHAR, driver.SPRAY_NOW_MSG)])

    async def test_action_waits_for_a_status_read_in_progress(self):
        """Operations run one at a time: a press goes after the read."""
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def reply(*_args):
            entered.set()
            await finish.wait()

        with patch.object(self.device, "_open_connection", AsyncMock()), \
             patch.object(self.device, "_set_time", AsyncMock()), \
             patch.object(driver, "SET_TIME_PACING", 0), \
             patch.object(self.device, "_await_status_reply", reply):
            poll = asyncio.create_task(self.device.connect())
            await entered.wait()
            press = asyncio.create_task(self.device.spray_now())
            await asyncio.sleep(0.05)
            written = [m for _, m in sent(self.client.write_gatt_char)]
            self.assertNotIn(driver.SPRAY_NOW_MSG, written)
            finish.set()
            self.assertTrue(await asyncio.wait_for(poll, 1))
            await asyncio.wait_for(press, 1)
            self.assertIn((driver.DEFAULT_COMMAND_CHAR, driver.SPRAY_NOW_MSG),
                          sent(self.client.write_gatt_char))
        self.client.disconnect.assert_not_awaited()
        self.assertIsNotNone(self.device._disconnect_task)

    async def test_a_command_ack_timeout_keeps_the_link_for_the_idle_disconnect(self):
        """Its ack would carry its own number, so it can't pass for a later
        command's: the link stays for the usual idle disconnect."""
        self.client.write_gatt_char.side_effect = None  # never acknowledged
        with self.assertRaises(driver.CommandNotConfirmed):
            await self.device._write_action(driver.SPRAY_NOW_MSG, 154, timeout=0.01)
        self.client.disconnect.assert_not_awaited()
        self.assertIsNotNone(self.device._disconnect_task)

    async def test_a_command_ack_timeout_closes_the_link_without_numbered_replies(self):
        """A firmware that doesn't copy numbers: Spray and Auto share an ack
        type, so the next command goes on a new link, out of the late
        ack's reach."""
        self.device._echoes_mn = False
        self.client.write_gatt_char.side_effect = None
        with self.assertRaises(driver.CommandNotConfirmed):
            await self.device._write_action(driver.SPRAY_NOW_MSG, 154, timeout=0.01)
        self.client.disconnect.assert_awaited_once()
        self.assertIsNone(self.device.client)

    async def test_a_cancelled_command_closes_the_link_without_numbered_replies(self):
        self.device._echoes_mn = False
        self.client.write_gatt_char.side_effect = None
        press = asyncio.create_task(self.device.spray_now())
        await asyncio.sleep(0.05)
        press.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await press
        self.client.disconnect.assert_awaited_once()
        self.assertIsNone(self.device.client)

    async def _fail_a_status_read(self):
        async def reply(*_args):
            raise driver.ConnectionError("No status")

        with patch.object(self.device, "_open_connection", AsyncMock()), \
             patch.object(self.device, "_set_time", AsyncMock()), \
             patch.object(driver, "SET_TIME_PACING", 0), \
             patch.object(self.device, "_await_status_reply", reply):
            with self.assertRaises(driver.ConnectionError):
                await self.device.connect()

    async def test_failed_status_read_leaves_the_link_to_the_idle_disconnect(self):
        """A late reply would carry its own number, so the link can stay."""
        await self._fail_a_status_read()
        self.client.disconnect.assert_not_awaited()
        self.assertIsNotNone(self.device._disconnect_task)

    async def test_failed_status_read_closes_the_link_without_numbered_replies(self):
        self.device._echoes_mn = False
        await self._fail_a_status_read()
        self.client.disconnect.assert_awaited_once()
        self.assertIsNone(self.device.client)

    async def _cancel_task(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_action_waits_while_a_poll_is_connecting(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def open_link(**kwargs):
            entered.set()
            await finish.wait()

        with patch.object(self.device, "_open_connection", open_link), \
             patch.object(self.device, "_set_time", AsyncMock()), \
             patch.object(driver, "SET_TIME_PACING", 0), \
             patch.object(self.device, "_await_status_reply", AsyncMock()):
            poll = asyncio.create_task(self.device.connect())
            await entered.wait()
            action = asyncio.create_task(self.device.spray_now())
            await asyncio.sleep(0)
            self.client.write_gatt_char.assert_not_awaited()
            finish.set()
            await poll
            await action
        self.assertIn((driver.DEFAULT_COMMAND_CHAR, driver.SPRAY_NOW_MSG),
                      sent(self.client.write_gatt_char))

    async def test_update_status_during_a_read_shares_its_result(self):
        entered = asyncio.Event()
        finish = asyncio.Event()
        opened = 0

        async def open_link(**kwargs):
            nonlocal opened
            opened += 1

        async def reply(*_args):
            entered.set()
            await finish.wait()

        with patch.object(self.device, "_open_connection", open_link), \
             patch.object(self.device, "_set_time", AsyncMock()), \
             patch.object(driver, "SET_TIME_PACING", 0), \
             patch.object(self.device, "_await_status_reply", reply):
            poll = asyncio.create_task(self.device.connect())
            await entered.wait()
            update = asyncio.create_task(self.device.connect())
            await asyncio.sleep(0)
            self.assertFalse(update.done())  # waits instead of failing
            finish.set()
            self.assertTrue(await poll)
            self.assertTrue(await update)
        self.assertEqual(opened, 1)  # one read, not two

    async def test_update_status_during_a_failing_read_fails_with_it(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def reply(*_args):
            entered.set()
            await finish.wait()
            raise driver.ConnectionError("No status")

        with patch.object(self.device, "_open_connection", AsyncMock()), \
             patch.object(self.device, "_set_time", AsyncMock()), \
             patch.object(driver, "SET_TIME_PACING", 0), \
             patch.object(self.device, "_await_status_reply", reply):
            poll = asyncio.create_task(self.device.connect())
            await entered.wait()
            update = asyncio.create_task(self.device.connect())
            await asyncio.sleep(0)
            finish.set()
            with self.assertRaises(driver.ConnectionError):
                await poll
            with self.assertRaises(driver.ConnectionError):
                await update

    async def test_overlapping_polls_are_refused(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def reply(*_args):
            entered.set()
            await finish.wait()

        with patch.object(self.device, "_open_connection", AsyncMock()), \
             patch.object(self.device, "_set_time", AsyncMock()), \
             patch.object(driver, "SET_TIME_PACING", 0), \
             patch.object(self.device, "_await_status_reply", reply):
            poll = asyncio.create_task(self.device.connect())
            await entered.wait()
            self.assertFalse(await self.device.connect_if_needed())
            finish.set()
            self.assertTrue(await poll)

    async def test_pending_disconnect_is_cancelled_before_action(self):
        await self.device.delayed_disconnect()
        old_task = self.device._disconnect_task
        await self.device.spray_now()
        await asyncio.sleep(0)
        self.assertTrue(old_task.cancelled())
        self.client.disconnect.assert_not_awaited()
        self.client.write_gatt_char.assert_awaited_once()

    async def test_poll_cancels_a_pending_disconnect(self):
        await self.device.delayed_disconnect()
        old_task = self.device._disconnect_task
        with patch.object(self.device, "_set_time", AsyncMock()), \
             patch.object(driver, "SET_TIME_PACING", 0), \
             patch.object(self.device, "_await_status_reply", AsyncMock()):
            await self.device.connect()
        await asyncio.sleep(0)
        self.assertTrue(old_task.cancelled())
        self.client.disconnect.assert_not_awaited()

    async def test_disconnect_waits_for_in_flight_action(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def write(*args):
            entered.set()
            await finish.wait()
            await self.confirm_write(*args)

        self.client.write_gatt_char.side_effect = write
        action = asyncio.create_task(self.device.spray_now())
        await entered.wait()
        disconnect = asyncio.create_task(self.device.disconnect())
        await asyncio.sleep(0)
        self.client.disconnect.assert_not_awaited()
        finish.set()
        await action
        await disconnect
        self.client.disconnect.assert_awaited_once()

    async def test_failed_write_is_reported_without_retry(self):
        self.client.write_gatt_char.side_effect = RuntimeError("Link lost")
        with self.assertRaises(driver.CommandNotConfirmed):
            await self.device.spray_now()
        self.client.write_gatt_char.assert_awaited_once()

    async def test_safe_write_reports_ble_error(self):
        self.client.write_gatt_char.side_effect = RuntimeError("Link lost")
        with self.assertRaisesRegex(driver.ConnectionError, "Link lost"):
            await self.device._write_message(driver.SPRAY_NOW_MSG)
        self.client.write_gatt_char.assert_awaited_once()

    async def test_safe_write_uses_the_requested_channel(self):
        await self.device._write_message(driver.SPRAY_NOW_MSG, char="other-channel")
        self.client.write_gatt_char.assert_awaited_once_with(
            "other-channel", driver.SPRAY_NOW_MSG)

    async def test_disconnect_waits_for_command_ack(self):
        written = asyncio.Event()

        async def write(*args):
            written.set()

        self.client.write_gatt_char.side_effect = write
        action = asyncio.create_task(self.device.spray_now())
        await written.wait()
        disconnect = asyncio.create_task(self.device.disconnect())
        await asyncio.sleep(0)
        self.client.disconnect.assert_not_awaited()
        self.device._handle_message(driver.encode_message(mT=154, mN=154, ack=200))
        await asyncio.wait_for(action, 1)
        await asyncio.wait_for(disconnect, 1)
        self.client.disconnect.assert_awaited_once()

    async def test_delayed_disconnect_does_not_cancel_itself(self):
        with patch.object(driver, "DISCONNECT_DELAY", 0):
            await self.device.delayed_disconnect()
            task = self.device._disconnect_task
            await asyncio.wait_for(task, 1)
        self.assertFalse(task.cancelled())
        self.assertIsNone(self.device._disconnect_task)
        self.client.disconnect.assert_awaited_once()

    async def test_background_refresh_waits_for_a_poll_in_progress(self):
        self.refresh_patch.stop()
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def reply(*_args):
            entered.set()
            await finish.wait()

        with patch.object(self.device, "_set_time", AsyncMock()), \
             patch.object(driver, "SET_TIME_PACING", 0), \
             patch.object(self.device, "_await_status_reply", reply), \
             patch.object(self.device, "get_device_status", AsyncMock()) as status:
            poll = asyncio.create_task(self.device.connect())
            await entered.wait()
            self.device._schedule_refresh()
            await asyncio.sleep(0.05)
            status.assert_not_awaited()
            finish.set()
            await asyncio.wait_for(poll, 1)
            await asyncio.wait_for(self.device._refresh_task, 1)
            status.assert_awaited_once()

    async def test_new_action_cancels_an_in_flight_refresh(self):
        self.refresh_patch.stop()
        entered = asyncio.Event()
        finish = asyncio.Event()
        calls = 0

        async def status(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await finish.wait()

        with patch.object(self.device, "get_device_status", status):
            self.device._schedule_refresh()
            old_refresh = self.device._refresh_task
            await entered.wait()
            await asyncio.wait_for(self.device.spray_now(), 1)
            await asyncio.wait_for(self.device._refresh_task, 1)
        self.assertTrue(old_refresh.cancelled())
        self.assertEqual(calls, 2)
        self.assertEqual(sent(self.client.write_gatt_char),
                         [(driver.DEFAULT_COMMAND_CHAR, driver.SPRAY_NOW_MSG)])


if __name__ == "__main__":
    unittest.main()
