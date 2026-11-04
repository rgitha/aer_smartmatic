"""SmartMatic against simulated devices for each known firmware variant."""
import asyncio
import functools

import pytest

from godrej import cbor
from godrej import godrej as gm
from godrej.const import MAIN_SVC, NOTIFY_CHAR
from godrej.exception import BatteryTooLow, CommandNotConfirmed
from godrej.exception import ConnectionError as DeviceConnectionError

from test_devicestatus import FRAME_SHIFTED, FRAME_308_IN

C1 = NOTIFY_CHAR
C2 = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
C3 = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
C4 = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
SPRAY_ACK = bytes.fromhex("bf626d54189a626d4e189a6361636b18c8ff")


class FakeChar:
    def __init__(self, uuid, handle, properties):
        self.uuid = uuid
        self.handle = handle
        self.properties = properties


class FakeService:
    def __init__(self, uuid, characteristics):
        self.uuid = uuid
        self.characteristics = characteristics

    def get_characteristic(self, uuid):
        return next((c for c in self.characteristics if c.uuid == uuid), None)


class FakeServices:
    def __init__(self, services):
        self._services = services

    def __iter__(self):
        return iter(self._services)

    def get_service(self, uuid):
        return next((s for s in self._services if s.uuid == uuid), None)


class FakeProxyApi:
    """The slice of aioesphomeapi's client that the raw fallback uses."""

    def __init__(self, device):
        self.device = device

    async def bluetooth_gatt_start_notify(self, address, handle, callback):
        uuid = self.device.uuid_by_handle[handle]
        if uuid in self.device.broken_notify:
            raise Exception("raw notify failed")
        self.device.callbacks[uuid] = lambda data: callback(handle, bytearray(data))
        self.device.raw_subscriptions.append(uuid)

        async def stop():
            self.device.callbacks.pop(uuid, None)

        return stop, lambda: self.device.callbacks.pop(uuid, None)


class FakeBackend:
    def __init__(self, device):
        self._client = FakeProxyApi(device)
        self._address_as_int = 0xA4C138CB0337
        self._notify_cancels = {}


class FakeDevice:
    """A Smart Matic that answers on ``command_char`` (one or a tuple).

    Commands are acknowledged (reply mT = request mT + 50) with ``ack``,
    or not at all if it is None. ``echo_mn``: the status reply carries the
    request's mN. ``reply_delay``: seconds before the status reply.

    ``via_proxy``: start_notify fails on characteristics without a CCCD
    (6e400001), as bleak-esphome does on v3 proxy connections.
    ``chunk``: notification payload size (ATT MTU - 3).
    ``needs_time``: stays silent until it has received a set-time message.
    ``lists_schedules``: answers the retrieve-schedules message (109), as
    fw 3.0.8-IN does; other firmwares stay silent. ``stores_schedules``:
    acknowledges the upload (103) as well.
    """

    def __init__(
        self, *, command_char, status_frame, reply_char=C1,
        via_proxy=True, chunk=182, needs_time=False, extra_chars=(),
        ack=200, echo_mn=False, reply_delay=0.0,
        lists_schedules=True, stores_schedules=True,
    ):
        self.lists_schedules = lists_schedules
        self.stores_schedules = stores_schedules
        self.command_char = command_char
        self.ack = ack
        self.echo_mn = echo_mn
        self.reply_delay = reply_delay
        self.status_frame = status_frame
        self.reply_char = reply_char
        self.via_proxy = via_proxy
        self.chunk = chunk
        self.needs_time = needs_time
        self.time_set = False

        chars = [
            FakeChar(C1, 0x10, ["read", "notify"]),
            FakeChar(C2, 0x13, ["read", "write-without-response", "notify"]),
            FakeChar(C3, 0x17, ["read", "write-without-response", "notify"]),
            FakeChar(C4, 0x1B, ["read", "write-without-response", "notify"]),
            *extra_chars,
        ]
        self.uuid_by_handle = {c.handle: c.uuid for c in chars}
        self.services = FakeServices([FakeService(MAIN_SVC, chars)])
        self.callbacks = {}
        self.raw_subscriptions = []
        self.subscribe_attempts = []
        self.broken_notify = set()
        self.writes = []
        self.stored_schedules = []  # 5-byte entries, as the device holds them

    def make_client(self, disconnected_callback):
        return FakeClient(self, disconnected_callback)

    def _notify(self, uuid, frame, delay=0.0):
        def send():
            callback = self.callbacks.get(uuid)
            if callback is None:
                return
            for i in range(0, len(frame), self.chunk):
                callback(frame[i:i + self.chunk])

        asyncio.get_running_loop().call_later(delay, send)

    def on_write(self, uuid, payload):
        self.writes.append((uuid, bytes(payload)))
        accepted = (
            self.command_char if isinstance(self.command_char, tuple)
            else (self.command_char,)
        )
        if uuid not in accepted:
            return
        message = cbor.loads(bytes(payload))
        mt = message["mT"]
        if mt == 102:  # set time
            self.time_set = True
        elif mt == 107:
            if self.status_frame and (self.time_set or not self.needs_time):
                frame = self.status_frame
                if self.echo_mn:
                    frame = frame.replace(
                        bytes.fromhex("626d4e189d"),
                        bytes.fromhex("626d4e18") + bytes([message["mN"]]), 1,
                    )
                self._notify(self.reply_char, frame, self.reply_delay)
        elif mt == 109 and self.ack is not None:
            if self.lists_schedules:
                self._notify(self.reply_char, schedule_reply(self.stored_schedules))
        elif mt == 103 and not self.stores_schedules:
            pass  # takes the upload, never answers
        elif self.ack is not None:
            if mt == 108:
                self.stored_schedules = []
            elif mt == 103:
                # Like fw 3.0.8-IN: entries over 144 sprays are acknowledged
                # but not stored.
                self.stored_schedules = [
                    bytes(e["s"]) for e in message["S"] if bytes(e["s"])[3] <= 144
                ]
            self._notify(
                self.reply_char,
                gm.encode_message(mT=mt + 50, mN=mt + 50, ack=self.ack),
            )


def schedule_reply(entries):
    """The device's answer to "retrieve schedules" (109 -> 159)."""
    out = bytearray([0xBF])
    for key, value in (("mT", 159), ("mN", 159), ("ack", 200)):
        out += gm._cbor_key(key) + gm._cbor_uint(value)
    out += gm._cbor_key("S") + bytes([0x9F])
    for entry in entries:
        out += bytes([0xBF]) + gm._cbor_key("s") + bytes([0x40 | len(entry)]) + entry + bytes([0xFF])
    out += bytes([0xFF, 0xFF])
    return bytes(out)


class FakeClient:
    def __init__(self, device, disconnected_callback):
        self.device = device
        self.services = device.services
        self.is_connected = True
        self.mtu_size = device.chunk + 3
        self._disconnected_callback = disconnected_callback
        if device.via_proxy:
            self._backend = FakeBackend(device)

    async def start_notify(self, char, callback):
        self.device.subscribe_attempts.append(char.uuid)
        if char.uuid in self.device.broken_notify:
            raise Exception("notify failed")
        if self.device.via_proxy and char.uuid == C1:
            raise Exception(
                f"Characteristic {char.uuid} does not have a characteristic "
                "client config descriptor."
            )

        def deliver(data):
            asyncio.get_running_loop().create_task(callback(char, bytearray(data)))

        self.device.callbacks[char.uuid] = deliver

    async def write_gatt_char(self, uuid, payload):
        self.device.on_write(uuid, payload)

    async def disconnect(self):
        self.is_connected = False


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(gm, "PROBE_TIMEOUT", 0.2)
    monkeypatch.setattr(gm, "ACK_TIMEOUT", 0.2)
    real_sleep = asyncio.sleep

    async def short_sleep(delay, *args, **kwargs):
        await real_sleep(min(delay, 0.01), *args, **kwargs)

    monkeypatch.setattr(gm.asyncio, "sleep", short_sleep)


def attach(monkeypatch, device, **kwargs):
    async def establish_connection(_cls, _ble_device, _mac, disconnected_callback, **_kwargs):
        return device.make_client(disconnected_callback)

    monkeypatch.setattr(gm, "establish_connection", establish_connection)
    return gm.SmartMatic(hass=None, mac="A4:C1:38:CB:03:37", **kwargs)


def probes(device):
    return [uuid for uuid, payload in device.writes if cbor.loads(payload)["mT"] == 107]


ALL = (C2, C3, C4)  # fw 3.0.8-IN takes commands on any of them


def short_status_timeout(monkeypatch, matic, timeout=0.2):
    """Make connect() (as called by connect_if_needed) give up quickly."""
    monkeypatch.setattr(
        matic, "connect", functools.partial(type(matic).connect, matic, status_timeout=timeout)
    )


# --- detection (adding a device, Reconfigure) -------------------------------

async def test_detect_accepts_working_defaults(monkeypatch):
    """Fw 3.0.8-IN through a proxy: the defaults answer, nothing is probed."""
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C2, C1)
    assert device.subscribe_attempts == [C1]  # only the default reply channel
    assert device.raw_subscriptions == [C1]  # via the CCCD-less fallback
    assert probes(device) == [C2]
    assert device.writes[0] == (C2, gm.FETCH_STATUS_MSG)  # the app's request
    assert device.time_set
    assert matic.device_status.firmware_version == "3.0.8-IN"
    assert matic.client is None  # disconnected afterwards


async def test_detect_falls_back_to_probing(monkeypatch):
    """A firmware that only takes commands on 0004."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C4, C1)
    assert probes(device) == [C2, C2, C3, C4]  # defaults, then each candidate
    assert set(device.subscribe_attempts) == {C1, C2, C3, C4}
    assert device.time_set


async def test_detect_other_frame_length_fragmented_on_local_adapter(monkeypatch):
    """Commands on 0003 only, 105-byte reply in 20-byte chunks."""
    device = FakeDevice(
        command_char=C3, status_frame=FRAME_SHIFTED, via_proxy=False, chunk=20,
    )
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C3, C1)
    assert device.raw_subscriptions == []
    assert matic.device_status.firmware_version == "3.0.10-T1x"


async def test_detect_finds_replies_on_another_characteristic(monkeypatch):
    device = FakeDevice(command_char=C2, reply_char=C2, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C2, C2)
    assert probes(device) == [C2, C2]


async def test_detect_device_that_needs_time_first(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN, needs_time=True)
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C4, C1)
    assert probes(device) == [C2, C2, C3, C4, C2, C3, C4]


async def test_detect_matches_late_replies_by_message_number(monkeypatch):
    """A reply that arrives after its probe timed out is credited correctly."""
    device = FakeDevice(
        command_char=C3, status_frame=FRAME_308_IN, echo_mn=True, reply_delay=0.3,
    )
    matic = attach(monkeypatch, device)

    # The reply to the 0003 probe only arrives while 0004 is being probed.
    assert await matic.detect_channels() == (C3, C1)


async def test_detect_without_replies(monkeypatch):
    device = FakeDevice(command_char=ALL, status_frame=b"")
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels(timeout=1) is None
    assert matic.client is None


async def test_detect_rejects_other_devices(monkeypatch):
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    device.services = FakeServices([])
    matic = attach(monkeypatch, device)

    with pytest.raises(gm.InvalidDeviceError):
        await matic.detect_channels()
    assert device.writes == []
    assert matic.client is None


async def test_detect_respects_the_time_budget(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)

    # 0.5s covers the defaults check and the 0002 probe, not 0004.
    assert await matic.detect_channels(timeout=0.5) is None
    assert C4 not in probes(device)
    assert matic.client is None


async def test_detect_on_a_running_instance_leaves_its_channels_alone(monkeypatch):
    """Reconfigure checks on a fresh link; the entry reload applies the result."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)
    assert device.subscribe_attempts == [C1]

    device.command_char = C3  # e.g. after a firmware update
    assert await matic.detect_channels() == (C3, C1)
    assert (matic.command_char, matic.reply_char) == (C4, C1)

    device.status_frame = b""
    assert await matic.detect_channels(timeout=1) is None
    assert (matic.command_char, matic.reply_char) == (C4, C1)


# --- runtime: saved channels are used as-is ---------------------------------

async def test_saved_channels_are_used_without_probing(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    assert await matic.connect(status_timeout=5)
    assert device.subscribe_attempts == [C1]
    assert matic.device_status.battery_mv == 3170
    await matic.spray_now()
    await matic._refresh_task
    assert {uuid for uuid, _ in device.writes} == {C4}


async def test_missed_reply_keeps_the_saved_channels(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)

    device.status_frame = b""  # e.g. weak signal
    with pytest.raises(DeviceConnectionError):
        await matic.get_device_status(timeout=0.2)
    assert (matic.command_char, matic.reply_char) == (C4, C1)
    assert probes(device) == [C4, C4]


async def test_broken_saved_reply_channel_has_no_fallback(monkeypatch):
    device = FakeDevice(command_char=C4, reply_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C3)
    device.broken_notify = {C3}

    with pytest.raises(DeviceConnectionError):
        await matic.connect(status_timeout=0.2)
    assert device.subscribe_attempts == [C3]
    assert (matic.command_char, matic.reply_char) == (C4, C3)


async def test_entry_without_saved_channels_uses_the_defaults(monkeypatch, caplog):
    """Devices added before detection existed use 0002/0001."""
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)
    caplog.set_level("WARNING", logger=gm.__name__)

    assert (matic.command_char, matic.reply_char) == (C2, C1)
    assert await matic.connect(status_timeout=5)
    assert device.subscribe_attempts == [C1]
    await matic.spray_now()
    await matic._refresh_task
    assert {uuid for uuid, _ in device.writes} == {C2}
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_actions_never_probe(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=b"")
    matic = attach(monkeypatch, device)

    for _ in range(2):
        device.writes.clear()
        with pytest.raises(CommandNotConfirmed):
            await matic.spray_now()
        assert device.writes[0] == (C2, gm.SPRAY_NOW_MSG)
    matic._refresh_task.cancel()
    assert set(probes(device)) <= {C2}


async def test_startup_failure_cooldown_and_logging(monkeypatch, caplog):
    device = FakeDevice(command_char=C4, status_frame=b"")
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    short_status_timeout(monkeypatch, matic)
    caplog.set_level("INFO", logger=gm.__name__)

    with pytest.raises(DeviceConnectionError):
        await matic.connect_if_needed()
    remaining = (matic._retry_after - gm.datetime.now()).total_seconds()
    assert 25 < remaining <= 30  # first retry soon
    assert [r.levelname for r in caplog.records if "Could not get status" in r.message] == ["WARNING"]

    # Repeated failures after the cooldown are not warned about again...
    caplog.clear()
    matic._retry_after = None
    with pytest.raises(DeviceConnectionError):
        await matic.connect_if_needed()
    assert not [r for r in caplog.records if r.levelname == "WARNING" and "Could not get status" in r.message]

    # ...and recovery is reported.
    device.status_frame = FRAME_308_IN
    matic._retry_after = None
    assert await matic.connect_if_needed()
    assert any("available again" in r.message for r in caplog.records)


# --- button presses ---------------------------------------------------------

async def test_press_subscribes_before_writing(monkeypatch):
    """The acknowledgement can only be caught with the reply channel open."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    subscribed_at_write = []
    on_write = device.on_write

    def record(uuid, payload):
        if payload == gm.SPRAY_NOW_MSG:
            subscribed_at_write.append(set(device.callbacks))
        on_write(uuid, payload)

    device.on_write = record
    await matic.spray_now()
    await matic._refresh_task

    assert subscribed_at_write == [{C1}]
    assert device.writes[0] == (C4, gm.SPRAY_NOW_MSG)
    assert matic.device_status.battery_mv == 3170


async def test_status_read_on_unsubscribed_connection_subscribes(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    async with matic._connect_lock:
        await matic._open_connection(subscribe=False)
    assert device.callbacks == {}
    assert await matic.connect(status_timeout=5)
    assert matic.device_status.battery_mv == 3170


async def test_press_returns_on_ack_before_read_back(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    await matic.spray_now()
    assert device.writes == [(C4, gm.SPRAY_NOW_MSG)]  # no read-back yet
    assert matic.device_status is None
    await matic._refresh_task
    assert matic.device_status.battery_mv == 3170


@pytest.mark.parametrize("action, mt", [
    (lambda m: m.spray_now(), 104),
    (lambda m: m.set_spray_interval(20), 104),
    (lambda m: m.stop_spray(), 106),
    (lambda m: m.reset_refill(), 105),
])
async def test_each_command_waits_for_its_own_ack(monkeypatch, caplog, action, mt):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    caplog.set_level("DEBUG", logger=gm.__name__)

    await action(matic)
    await matic._refresh_task
    assert cbor.loads(device.writes[0][1])["mT"] == mt
    assert any(r.message.endswith(f"acknowledged by {matic.mac}") for r in caplog.records)
    assert matic._ack_waiters == {}


async def test_missing_ack_raises_and_still_reads_back(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN, ack=None)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    with pytest.raises(CommandNotConfirmed):
        await matic.stop_spray()
    await matic._refresh_task
    assert probes(device) == [C4]  # the read-back shows what happened
    assert matic.device_status.battery_mv == 3170
    assert matic._ack_waiters == {}


async def test_rejected_command_raises(monkeypatch, caplog):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN, ack=500)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    caplog.set_level("WARNING", logger=gm.__name__)

    with pytest.raises(CommandNotConfirmed):
        await matic.reset_refill()
    await matic._refresh_task
    assert any("Refill reset rejected" in r.message for r in caplog.records)


async def test_failed_write_raises(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    on_write = device.on_write

    def fail_spray(uuid, payload):
        if payload == gm.SPRAY_NOW_MSG:
            raise Exception("write failed")
        on_write(uuid, payload)

    device.on_write = fail_spray
    with pytest.raises(CommandNotConfirmed):
        await matic.spray_now()
    await matic._refresh_task


async def test_quick_presses_are_each_confirmed(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    for _ in range(3):
        await matic.spray_now()
    await matic._refresh_task
    sprays = [u for u, p in device.writes if p == gm.SPRAY_NOW_MSG]
    assert sprays == [C4, C4, C4]
    assert matic.device_status.battery_mv == 3170


async def test_overlapping_commands_are_each_confirmed(monkeypatch):
    """Spray and Auto Spray share an ack type; overlapping presses mustn't steal acks."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    await asyncio.gather(
        matic.spray_now(), matic.set_spray_interval(20), matic.spray_now(), matic.stop_spray()
    )
    await matic._refresh_task
    assert len([w for w in device.writes if cbor.loads(w[1])["mT"] != 107]) == 4
    assert matic._ack_waiters == {}


async def test_unreachable_device_fails_queued_commands_at_once(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    calls = []

    async def fail(*args, max_attempts=None, **kwargs):
        calls.append(max_attempts)
        await asyncio.sleep(0)
        raise Exception("Timeout waiting for connect response")

    monkeypatch.setattr(gm, "establish_connection", fail)
    results = await asyncio.gather(
        matic.stop_spray(), matic.spray_now(), matic.reset_refill(),
        return_exceptions=True,
    )
    assert all(isinstance(r, DeviceConnectionError) for r in results)
    assert calls == [1]  # one single-attempt connect for all three
    assert matic.reachable is False

    # A later press tries again.
    with pytest.raises(DeviceConnectionError):
        await matic.spray_now()
    assert len(calls) == 2


async def test_press_gives_up_connecting_after_the_time_limit(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=gm.__name__)
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "CONNECT_TIMEOUT", 0.1)
    never = asyncio.Event()

    async def hang(*args, **kwargs):
        await never.wait()  # the proxy never gets a connection

    monkeypatch.setattr(gm, "establish_connection", hang)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(DeviceConnectionError):
        await matic.spray_now()
    assert loop.time() - start < 1
    assert matic.reachable is False
    assert any("(command): no connection within 0.1 s" in r.message for r in caplog.records)


async def test_poll_waits_for_the_whole_attempt(monkeypatch):
    """Nobody waits on a background read: it lets the library end the
    attempt instead of cutting it short."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "CONNECT_TIMEOUT", 0.05)
    calls = []

    async def library_gives_up(*args, max_attempts=None, **kwargs):
        calls.append(max_attempts)
        await asyncio.wait_for(asyncio.Event().wait(), 0.3)  # its own timeout

    monkeypatch.setattr(gm, "establish_connection", library_gives_up)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(DeviceConnectionError):
        await matic.connect_if_needed()
    assert 0.25 < loop.time() - start < 1
    assert calls == [1]


async def test_press_gives_up_but_the_attempt_carries_on(monkeypatch):
    """The press answers quickly; the attempt isn't cancelled, and a link
    that comes up after nobody is waiting is closed, without the command."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "CONNECT_TIMEOUT", 0.05)
    connect = gm.establish_connection
    release = asyncio.Event()
    cancelled = []
    clients = []

    async def late(*args, **kwargs):
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        client = await connect(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(gm, "establish_connection", late)
    with pytest.raises(DeviceConnectionError):
        await matic.spray_now()
    attempt = matic._link_task
    assert attempt is not None and not attempt.done()  # still trying

    release.set()
    await attempt
    for _ in range(5):
        await asyncio.sleep(0)
    assert not cancelled
    assert clients and not clients[0].is_connected  # the late link was closed
    assert matic.client is None
    assert gm.SPRAY_NOW_MSG not in [p for _, p in device.writes]  # not sent late


async def test_a_press_during_a_slow_background_read_answers_quickly(monkeypatch):
    """The background read waits for the link without holding the lock, so
    a press joins the same attempt and still answers within its limit."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "CONNECT_TIMEOUT", 0.05)
    never = asyncio.Event()
    calls = []

    async def hang(*args, **kwargs):
        calls.append(1)
        await never.wait()

    monkeypatch.setattr(gm, "establish_connection", hang)
    poll = asyncio.ensure_future(matic.connect_if_needed())
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(DeviceConnectionError):
        await matic.spray_now()
    assert loop.time() - start < 0.5
    assert calls == [1]  # joined, not a second attempt
    poll.cancel()
    matic._link_task.cancel()


async def test_unloading_ends_an_attempt_in_flight(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "CONNECT_TIMEOUT", 0.05)
    never = asyncio.Event()

    async def hang(*args, **kwargs):
        await never.wait()

    monkeypatch.setattr(gm, "establish_connection", hang)
    with pytest.raises(DeviceConnectionError):
        await matic.spray_now()
    attempt = matic._link_task
    await matic.shutdown()
    await asyncio.sleep(0)
    assert attempt.cancelled()


async def test_a_second_press_joins_the_attempt(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "CONNECT_TIMEOUT", 0.05)
    never = asyncio.Event()
    calls = []

    async def hang(*args, **kwargs):
        calls.append(1)
        await never.wait()

    monkeypatch.setattr(gm, "establish_connection", hang)
    for _ in range(2):
        with pytest.raises(DeviceConnectionError):
            await matic.spray_now()
    assert calls == [1]  # one attempt, joined by the second press
    matic._link_task.cancel()


async def test_repeated_failures_while_heard_warn_once(monkeypatch, caplog):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "STUCK_AFTER", 2)
    matic.set_advertising(True)
    caplog.set_level("WARNING", logger=gm.__name__)

    async def refused(*args, **kwargs):
        raise Exception("Timeout waiting for connect response")

    monkeypatch.setattr(gm, "establish_connection", refused)
    for _ in range(3):
        with pytest.raises(DeviceConnectionError):
            await matic.spray_now()
        await asyncio.sleep(0)
    warnings = [r.message for r in caplog.records if "restart that adapter or proxy" in r.message]
    assert len(warnings) == 1


async def test_press_during_a_failing_poll_fails_with_it(monkeypatch):
    """A press waits for a poll that is connecting, not for a second attempt."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    release = asyncio.Event()
    calls = []

    async def slow_fail(*args, **kwargs):
        calls.append(1)
        await release.wait()
        raise Exception("Timeout waiting for connect response")

    monkeypatch.setattr(gm, "establish_connection", slow_fail)
    poll = asyncio.ensure_future(matic.connect_if_needed())
    await asyncio.sleep(0)
    press = asyncio.ensure_future(matic.spray_now())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(DeviceConnectionError):
        await poll
    with pytest.raises(DeviceConnectionError):
        await press
    assert calls == [1]  # the press did not start its own attempt


async def test_press_during_a_successful_poll_uses_its_link(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    poll = asyncio.ensure_future(matic.connect(status_timeout=5))
    await asyncio.sleep(0)
    await matic.spray_now()  # confirmed on the poll's link
    assert await poll
    assert [u for u, p in device.writes if p == gm.SPRAY_NOW_MSG] == [C4]
    if matic._refresh_task is not None:
        matic._refresh_task.cancel()


@pytest.mark.parametrize("action", [
    lambda m: m.spray_now(), lambda m: m.set_spray_interval(10),
])
async def test_spray_below_the_cut_off_is_reported(monkeypatch, caplog, action):
    """The device acks sprays it can't do; the last status tells."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)
    matic.device_status.battery_mv = 2390
    caplog.set_level("WARNING", logger=gm.__name__)

    with pytest.raises(BatteryTooLow):
        await action(matic)
    assert any("2390 mV" in r.message for r in caplog.records)
    matic._refresh_task.cancel()


async def test_non_spray_commands_ignore_the_battery(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)
    matic.device_status.battery_mv = 2390

    await matic.stop_spray()
    await matic.reset_refill()
    matic._refresh_task.cancel()


async def test_poll_backoff_grows_and_resets(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=b"")
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    short_status_timeout(monkeypatch, matic)
    waits = []
    for _ in range(5):
        matic._retry_after = None
        with pytest.raises(DeviceConnectionError):
            await matic.connect_if_needed()
        waits.append(round((matic._retry_after - gm.datetime.now()).total_seconds() / 10) * 10)
    assert waits == [30, 60, 120, 300, 300]

    device.status_frame = FRAME_308_IN
    matic._retry_after = None
    assert await matic.connect_if_needed()
    matic.device_status = None
    device.status_frame = b""
    with pytest.raises(DeviceConnectionError):
        await matic.connect_if_needed()
    assert (matic._retry_after - gm.datetime.now()).total_seconds() <= 30


async def test_background_check_recovers_after_a_failed_press(monkeypatch):
    """A recent status mustn't stop the device being checked again."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)
    await matic.disconnect()
    assert not await matic.connect_if_needed()  # fresh status: nothing to do

    real = gm.establish_connection

    async def off(*args, **kwargs):
        raise Exception("Timeout waiting for connect response")

    monkeypatch.setattr(gm, "establish_connection", off)
    with pytest.raises(DeviceConnectionError):
        await matic.stop_spray()  # device switched off
    assert matic.reachable is False

    monkeypatch.setattr(gm, "establish_connection", real)  # back on
    assert await matic.connect_if_needed()
    assert matic.reachable is True
    assert not await matic.connect_if_needed()  # and quiet again


async def test_press_during_a_slow_status_reply_is_not_held_up(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN, reply_delay=0.5)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    monkeypatch.setattr(gm, "SET_TIME_PACING", 0)
    loop = asyncio.get_running_loop()

    poll = asyncio.ensure_future(matic.connect(status_timeout=5))
    while not any(cbor.loads(p)["mT"] == 107 for _, p in device.writes):
        await asyncio.sleep(0.01)  # the poll is now waiting for its reply
    start = loop.time()
    await matic.spray_now()
    assert loop.time() - start < 0.3  # well before the 0.5 s reply
    assert await poll
    matic._refresh_task.cancel()


async def test_press_cancels_a_pending_disconnect(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)
    pending = matic._disconnect_task
    assert pending is not None and not pending.done()

    await matic.spray_now()
    await asyncio.sleep(0)
    assert pending.cancelled()
    await matic._refresh_task


# --- notification handling --------------------------------------------------

async def test_stale_fragment_does_not_corrupt_next_frame(monkeypatch):
    matic = gm.SmartMatic(hass=None, mac="A4:C1:38:CB:03:37")
    clock = [1000.0]
    monkeypatch.setattr(gm.time, "monotonic", lambda: clock[0])

    matic._on_notification(C1, FRAME_308_IN[:30])  # partial, never finished
    clock[0] += gm._FRAGMENT_TIMEOUT + 1
    matic._on_notification(C1, FRAME_308_IN)
    assert matic.device_status is not None and matic.device_status.is_valid
    assert matic._rx_buffers == {}


async def test_acks_do_not_count_as_status(monkeypatch):
    matic = gm.SmartMatic(hass=None, mac="A4:C1:38:CB:03:37")
    matic._on_notification(C1, SPRAY_ACK)
    assert matic.device_status is None
    assert not matic._device_status_event.is_set()


# --- reachability -----------------------------------------------------------

async def test_reachable_survives_the_normal_disconnect(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert matic.reachable is None and matic.last_update is None

    assert await matic.connect(status_timeout=5)
    assert matic.reachable is True
    assert matic.last_update is not None and matic.last_update.tzinfo is not None

    await matic.disconnect()  # the routine drop after an exchange
    assert matic.reachable is True


async def test_reachable_turns_off_on_real_trouble_and_back_on(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    short_status_timeout(monkeypatch, matic)
    events = []
    matic.eventbus.add_listener(gm.DEVICE_REACHABILITY, AsyncMockRecorder(events))

    assert await matic.connect_if_needed()
    first = matic.last_update

    device.status_frame = b""  # stops answering
    matic.device_status = None
    with pytest.raises(DeviceConnectionError):
        await matic.connect_if_needed()
    assert matic.reachable is False
    assert matic.last_update == first  # unchanged by the failure

    device.status_frame = FRAME_308_IN
    matic._retry_after = None
    assert await matic.connect_if_needed()
    assert matic.reachable is True
    await asyncio.sleep(0)
    assert events == [True, False, True]  # one event per change only


async def test_reachable_follows_advertisements(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    assert await matic.connect(status_timeout=5)

    matic.set_advertising(False)  # out of range / flat battery
    assert matic.reachable is False
    matic.set_advertising(True)
    assert matic.reachable is True


async def test_heard_changes_are_announced_after_a_failed_exchange(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    events = []
    matic.eventbus.add_listener(gm.DEVICE_REACHABILITY, AsyncMockRecorder(events))
    matic._set_exchange_ok(False)  # Connection status: Unreachable
    await asyncio.sleep(0)
    events.clear()

    # Unreachable throughout, but entities must still hear heard change.
    matic.set_advertising(False)
    matic.set_advertising(False)
    matic.set_advertising(True)
    await asyncio.sleep(0)
    assert matic.reachable is False
    assert len(events) == 2


async def test_failed_press_marks_unreachable(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    async def fail(*args, **kwargs):
        raise Exception("out of range")

    monkeypatch.setattr(gm, "establish_connection", fail)
    with pytest.raises(DeviceConnectionError):
        await matic.spray_now()
    assert matic.reachable is False


class AsyncMockRecorder:
    def __init__(self, sink):
        self.sink = sink

    async def __call__(self, matic):
        self.sink.append(matic.reachable)


async def test_detect_reports_each_stage(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)
    stages = []

    assert await matic.detect_channels(progress=lambda *a: stages.append(a)) == (C4, C1)
    assert stages == [
        ("connecting", {}, 0.0),
        ("checking_defaults", {}, 0.0),
        ("probing", {"index": "1", "total": "3", "channel": "6e400002"}, 0.0),
        ("probing", {"index": "2", "total": "3", "channel": "6e400003"}, 1 / 6),
        ("probing", {"index": "3", "total": "3", "channel": "6e400004"}, 2 / 6),
    ]


C5 = "6e400005-b5a3-f393-e0a9-e50e24dcca9e"
C6 = "6e400006-b5a3-f393-e0a9-e50e24dcca9e"


async def test_detect_probes_unknown_writable_characteristics_last(monkeypatch):
    """A firmware that takes commands somewhere new is still found."""
    device = FakeDevice(
        command_char=C5, status_frame=FRAME_308_IN,
        extra_chars=(
            FakeChar(C6, 0x23, ["read", "notify"]),               # not writable: skipped
            FakeChar(C5, 0x1F, ["read", "write-without-response"]),
        ),
    )
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C5, C1)
    assert probes(device) == [C2, C2, C3, C4, C5]


async def test_detect_skips_known_channels_the_device_lacks(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    device.services = FakeServices([FakeService(MAIN_SVC, [
        FakeChar(C1, 0x10, ["read", "notify"]),
        FakeChar(C4, 0x1B, ["read", "write-without-response", "notify"]),
    ])])
    matic = attach(monkeypatch, device)

    assert await matic.detect_channels() == (C4, C1)
    assert probes(device) == [C2, C4]  # the default is tried regardless


class CountingService(FakeService):
    """Counts how often the characteristic list is walked."""

    def __init__(self, uuid, characteristics):
        self._chars = characteristics
        self.uuid = uuid
        self.walks = 0

    @property
    def characteristics(self):
        self.walks += 1
        return self._chars

    def get_characteristic(self, uuid):
        return next((c for c in self._chars if c.uuid == uuid), None)


async def test_normal_connection_does_not_walk_the_characteristics(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    service = CountingService(MAIN_SVC, device.services.get_service(MAIN_SVC).characteristics)
    device.services = FakeServices([service])
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)

    assert await matic.connect(status_timeout=5)
    await matic.spray_now()
    await matic._refresh_task
    assert service.walks == 0
    assert device.subscribe_attempts == [C1]
    assert matic.device_status.is_valid


_GATED = 99.0  # the Identify wait the gated sleep recognises


def idle_identify(monkeypatch, device, *, gate_wait=False):
    """A SmartMatic that reads back idle, ready to be asked to identify.

    ``gate_wait``: Identify's wait blocks until the returned event is set,
    so a test can act while the lights are on.
    """
    from unittest.mock import AsyncMock
    from godrej.devicestatus import DeviceStatus

    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    idle = DeviceStatus(gm.encode_message(mT=157, mN=157, ack=200, bV=3100, cS=0))

    async def fresh_status(**_):
        matic.device_status = idle
    matic.connect = AsyncMock(side_effect=fresh_status)

    if not gate_wait:
        monkeypatch.setattr(gm, "IDENTIFY_SECONDS", 0)
        return matic, None, None

    lit = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(gm, "IDENTIFY_SECONDS", _GATED)
    real_sleep = asyncio.sleep

    async def gated_sleep(delay, *args, **kwargs):
        if delay is _GATED:
            lit.set()
            await release.wait()
            return
        await real_sleep(min(delay, 0.01), *args, **kwargs)

    monkeypatch.setattr(gm.asyncio, "sleep", gated_sleep)
    return matic, lit, release


def commands(device):
    """The message types written, without the status requests."""
    types = [cbor.loads(payload)["mT"] for _, payload in device.writes]
    return [mt for mt in types if mt != 107]


async def test_identify_lights_then_clears_while_idle(monkeypatch):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic, _, _ = idle_identify(monkeypatch, device)

    await matic.identify()
    await matic._refresh_task
    assert commands(device) == [113, 106]  # LED test, then auto off to clear


async def test_identify_does_not_stop_an_interval_picked_during_its_wait(monkeypatch):
    """The auto off that clears the lights must not undo a fresh interval."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic, lit, release = idle_identify(monkeypatch, device, gate_wait=True)

    identifying = asyncio.create_task(matic.identify())
    await asyncio.wait_for(lit.wait(), 1)
    picking = asyncio.create_task(matic.set_spray_interval(10))
    for _ in range(20):  # the interval command waits its turn
        await asyncio.sleep(0)
    assert commands(device) == [113]  # LED test only

    release.set()
    await asyncio.wait_for(identifying, 1)
    await asyncio.wait_for(picking, 1)
    await matic._refresh_task
    # LED test, the auto off clearing the lights, then every 10 min
    assert commands(device) == [113, 106, 104]


async def test_identify_clears_the_lights_when_the_test_is_not_confirmed(monkeypatch):
    """The device may light them anyway, so auto off is sent regardless."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN, ack=None)
    matic, _, _ = idle_identify(monkeypatch, device)

    with pytest.raises(CommandNotConfirmed):
        await matic.identify()
    assert commands(device) == [113, 106]


async def test_identify_clears_the_lights_when_its_wait_is_cancelled(monkeypatch):
    """As on an HA shutdown; the lights would stay lit until a mode command."""
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic, lit, _ = idle_identify(monkeypatch, device, gate_wait=True)

    identifying = asyncio.create_task(matic.identify())
    await asyncio.wait_for(lit.wait(), 1)
    identifying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await identifying
    assert commands(device) == [113, 106]
    await matic._refresh_task


async def test_identify_refuses_unless_idle(monkeypatch):
    from unittest.mock import AsyncMock
    from godrej.devicestatus import DeviceStatus
    from godrej.exception import NotIdle

    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    busy = DeviceStatus(gm.encode_message(mT=157, mN=157, ack=200, bV=3100, cS=121))

    async def fresh_status(**_):
        matic.device_status = busy
    matic.connect = AsyncMock(side_effect=fresh_status)
    with pytest.raises(NotIdle) as err:
        await matic.identify()
    assert err.value.schedule  # cS 121: started by a schedule
    assert device.writes == []


# --- the device's own schedules ---------------------------------------------

from datetime import time as dtime  # noqa: E402

from godrej.schedule import Schedule, ScheduleError  # noqa: E402


def sched(days="daily", start="08:00", end="22:30", interval=40):
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    return Schedule.create(days, dtime(h1, m1), dtime(h2, m2), interval)


def test_schedule_bytes_match_the_app():
    s = sched()  # daily 08:00-22:30 every 40 min
    assert s.to_bytes() == bytes([0x7F, 8, 0, 21, 40])  # 870 min // 40 = 21 sprays
    assert f"{s.end:%H:%M}" == "22:00"  # rounded down to a whole interval
    weekdays = sched(["mon", "tue", "wed", "thu", "fri"], "07:00", "14:00", 10)
    assert weekdays.to_bytes() == bytes([0b1111100, 7, 0, 42, 10])
    assert Schedule.from_bytes(weekdays.to_bytes()) == weekdays
    assert sched(["sun", "Sat"]).days == ("sat", "sun")


def test_set_schedules_message_matches_the_app():
    assert gm.encode_schedules([sched()]) == bytes.fromhex(
        "bf626d541867626d4e18996153"  # {mT: 103, mN: 153, S:
        "9fbf6173457f08001528ff"      #   [{s: h'7F 08 00 15 28'}
        "ffff"                        #   ]}
    )


@pytest.mark.parametrize("make, message", [
    (lambda: sched(start="08:00", end="08:15", interval=20), "shorter than one"),
    (lambda: sched(interval=15), "Interval must be"),
    (lambda: sched(days=["someday"]), "Unknown day"),
    (lambda: sched(days=[]), "at least one day"),
])
def test_impossible_schedules_are_refused(make, message):
    with pytest.raises(ScheduleError, match=message):
        make()


def test_lists_the_device_cannot_hold_are_refused():
    with pytest.raises(ScheduleError, match="overlap"):
        gm.validate_schedules([sched("mon", "08:00", "12:00"), sched("daily", "11:00", "13:00")])
    gm.validate_schedules([sched("mon", "08:00", "12:00"), sched("tue", "11:00", "13:00")])
    many = [sched("daily", f"{h:02d}:00", f"{h:02d}:40", 40) for h in range(11)]
    with pytest.raises(ScheduleError, match="at most 10"):
        gm.validate_schedules(many)


def schedule_writes(device):
    return [(uuid, cbor.loads(p)["mT"]) for uuid, p in device.writes
            if cbor.loads(p)["mT"] in (103, 108, 109)]


async def test_set_schedules_replaces_the_list_like_the_app(monkeypatch):
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)
    events = []
    matic.eventbus.add_listener(gm.DEVICE_SCHEDULES_UPDATE, AsyncMockRecorder(events))

    wanted = [sched(["mon", "tue"], "07:00", "14:00", 10), sched("daily", "16:00", "23:00", 20)]
    result = await matic.set_schedules(wanted)
    assert result == wanted and matic.schedules == wanted
    # Remove all, upload on 6e400004, read back.
    assert schedule_writes(device) == [(C2, 108), (C4, 103), (C2, 109)]
    assert device.stored_schedules == [s.to_bytes() for s in wanted]
    await asyncio.sleep(0)
    assert events


async def test_schedules_follow_the_acknowledgements_when_not_read_back(monkeypatch):
    """A device that doesn't list its schedules still shows the list it
    acknowledged, not the old one."""
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN, lists_schedules=False)
    matic = attach(monkeypatch, device)
    matic.schedules = [sched("daily", "06:00", "07:00", 20)]
    wanted = [sched(["mon"], "07:00", "10:00", 20)]
    assert await matic.set_schedules(wanted) == wanted
    assert matic.schedules == wanted


async def test_schedules_are_empty_once_remove_all_is_acknowledged(monkeypatch):
    """The upload then goes unanswered and the list can't be read: the
    device is known to be empty, so that is what is shown."""
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(
        command_char=ALL, status_frame=FRAME_308_IN,
        lists_schedules=False, stores_schedules=False,
    )
    matic = attach(monkeypatch, device)
    matic.schedules = [sched("daily", "06:00", "07:00", 20)]
    with pytest.raises(Exception):
        await matic.set_schedules([sched(["mon"], "07:00", "10:00", 20)])
    assert matic.schedules == []


async def test_saving_schedules_reads_the_status_back(monkeypatch):
    # Saving stops a running schedule, so the status is read again after.
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)
    refresh = []
    monkeypatch.setattr(matic, "_schedule_refresh", lambda: refresh.append(1))
    await matic.set_schedules([sched("daily", "16:00", "23:00", 20)])
    assert refresh == [1]


async def test_saving_a_schedule_covering_now_watches_for_its_start(monkeypatch):
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)
    monkeypatch.setattr(matic, "_schedule_refresh", lambda: None)
    watched = []
    monkeypatch.setattr(matic, "_watch_schedule_start", lambda: watched.append(1))
    now = gm.dt_util.now()
    day = gm.DAYS[now.weekday()] if hasattr(gm, "DAYS") else ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]
    start = f"{(now.hour - 1) % 24:02d}:00"
    end = f"{(now.hour + 1) % 24:02d}:00"
    covering = sched([day], start, end, 20)
    await matic.set_schedules([covering])
    assert watched == [1]
    watched.clear()
    later = sched([day], f"{(now.hour + 3) % 24:02d}:00", f"{(now.hour + 4) % 24:02d}:00", 20)
    await matic.set_schedules([later])
    assert watched == []  # nothing will start: no follow-up reads


async def test_the_idle_disconnect_keeps_watching_for_a_schedule_start(monkeypatch):
    """The routine disconnect after an exchange (about 15 s after a save)
    must not stop the reads at 30, 60 and 90 s; unloading does."""
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)
    matic._watch_schedule_start()
    watch = matic._start_watch
    await matic.disconnect()
    await asyncio.sleep(0)
    assert not watch.done()
    await matic.shutdown()
    await asyncio.sleep(0)
    assert watch.cancelled()


async def test_a_failed_update_status_counts_as_a_failed_exchange(monkeypatch):
    """Like a failed command: Connection status shows it, and the
    background check retries instead of waiting for the hourly read."""
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)
    await matic.connect()
    assert matic._last_exchange_ok is True

    async def refused(*args, **kwargs):
        raise Exception("Timeout waiting for connect response")

    await matic.disconnect()
    monkeypatch.setattr(gm, "establish_connection", refused)
    with pytest.raises(DeviceConnectionError):
        await matic.connect(purpose="update status")
    assert matic._last_exchange_ok is False


async def test_schedules_are_read_again_after_a_failed_read(monkeypatch):
    """A schedule read that goes unanswered is tried again after the next
    successful status read, not left until the next save or restart."""
    monkeypatch.setattr(gm, "SCHEDULE_REPLY_TIMEOUT", 0.05)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN, lists_schedules=False)
    matic = attach(monkeypatch, device)
    await matic.connect()
    assert await matic.read_schedules_quietly() is None
    assert matic._schedules_stale

    device.lists_schedules = True
    matic.device_status = None  # due for a background read
    assert await matic.connect_if_needed()
    await matic._stale_read
    assert not matic._schedules_stale
    assert matic.schedules == []


async def test_clearing_schedules_skips_the_upload(monkeypatch):
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    device.stored_schedules = [sched().to_bytes()]
    matic = attach(monkeypatch, device)

    assert await matic.set_schedules([]) == []
    assert [mt for _, mt in schedule_writes(device)] == [108, 109]
    assert device.stored_schedules == []


async def test_add_and_remove_work_on_what_the_device_holds(monkeypatch):
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    first = sched("mon", "08:00", "10:00", 20)
    device.stored_schedules = [first.to_bytes()]  # e.g. set up elsewhere
    matic = attach(monkeypatch, device)

    second = sched("tue", "08:00", "10:00", 20)
    assert await matic.add_schedule(second) == [first, second]
    assert await matic.remove_schedule(1) == [second]
    with pytest.raises(ScheduleError, match="no schedule 5"):
        await matic.remove_schedule(5)


async def test_edits_do_not_overwrite_each_other(monkeypatch):
    """Two edits at once: the second works on what the first left."""
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)

    monday = sched("mon", "08:00", "10:00", 20)
    tuesday = sched("tue", "08:00", "10:00", 20)
    await asyncio.gather(matic.add_schedule(monday), matic.add_schedule(tuesday))
    assert sorted(device.stored_schedules) == sorted([monday.to_bytes(), tuesday.to_bytes()])


async def test_a_failed_upload_leaves_the_emptied_list_showing(monkeypatch):
    """The remove went through, so the stale entries must not linger."""
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    monkeypatch.setattr(gm, "SCHEDULE_REPLY_TIMEOUT", 0.05)
    device = FakeDevice(
        command_char=ALL, status_frame=FRAME_308_IN, stores_schedules=False
    )
    device.stored_schedules = [sched().to_bytes()]
    matic = attach(monkeypatch, device)
    await matic.read_schedules()

    with pytest.raises(CommandNotConfirmed):
        await matic.set_schedules([sched("mon", "08:00", "10:00", 20)])
    assert matic.schedules == []  # read back after the failure


async def test_a_firmware_that_never_lists_schedules_can_still_be_set(monkeypatch, caplog):
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    monkeypatch.setattr(gm, "SCHEDULE_REPLY_TIMEOUT", 0.05)
    device = FakeDevice(
        command_char=ALL, status_frame=FRAME_308_IN, lists_schedules=False
    )
    matic = attach(monkeypatch, device)

    wanted = [sched("mon", "08:00", "10:00", 20)]
    assert await matic.set_schedules(wanted) == wanted
    assert device.stored_schedules == [s.to_bytes() for s in wanted]
    assert "Could not read the schedules" in caplog.text


async def test_rejected_upload_raises(monkeypatch):
    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN, ack=500)
    matic = attach(monkeypatch, device)
    with pytest.raises(CommandNotConfirmed):
        await matic.set_schedules([sched()])


@pytest.mark.parametrize("target, mt, key, code", [
    ("current", 106, "sS", 128), ("all", 106, "sS", 255), (3, 106, "sS", 2),
])
async def test_stop_schedule_message(monkeypatch, target, mt, key, code):
    device = FakeDevice(command_char=C4, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device, command_char=C4, reply_char=C1)
    await matic.stop_schedule(target)
    await matic._refresh_task
    sent = cbor.loads(device.writes[0][1])
    assert (sent["mT"], sent[key]) == (mt, code)
    assert sent[key] is not True  # a number, unlike "auto spray off"


async def test_set_schedules_checks_the_expected_list_first(monkeypatch):
    from godrej.exception import SchedulesChanged

    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    device.stored_schedules = [sched().to_bytes()]
    matic = attach(monkeypatch, device)
    stale = [{"days": ["Sun"], "start": "06:00", "end": "07:00", "interval": 20}]
    with pytest.raises(SchedulesChanged):
        await matic.set_schedules([], expected=stale)
    assert [cbor.loads(p)["mT"] for _, p in device.writes] == [109]  # read only, nothing written

    device.writes.clear()
    current = [s.as_dict() for s in matic.schedules]
    await matic.set_schedules([], expected=current)
    assert [cbor.loads(p)["mT"] for _, p in device.writes][:2] == [109, 108]


def test_running_schedule_is_found_from_status_and_list():
    from datetime import datetime
    from godrej.devicestatus import DeviceStatus

    matic = gm.SmartMatic.__new__(gm.SmartMatic)
    matic.schedules = [
        Schedule.create("daily", dtime(8, 0), dtime(10, 0), 20),
        Schedule.create(["sat"], dtime(12, 0), dtime(14, 0), 10),
    ]
    matic.device_status = DeviceStatus(gm.encode_message(mT=157, mN=157, ack=200, bV=3100, cS=121))
    saturday = datetime(2026, 10, 31, 12, 30)
    assert matic.running_schedule(saturday) == 2  # cS 121: schedule, every 10 min
    assert matic.running_schedule(datetime(2026, 10, 31, 14, 1)) == 2  # a minute past the end
    assert matic.running_schedule(datetime(2026, 10, 31, 15, 0)) is None
    matic.device_status = DeviceStatus(gm.encode_message(mT=157, mN=157, ack=200, bV=3100, cS=0))
    assert matic.running_schedule(saturday) is None



def test_windows_past_midnight_run_into_the_next_day():
    late = sched(start="23:00", end="01:00", interval=40)  # ends the next day
    assert (late.sprays, late.next_day, str(late.end)) == (3, True, "01:00:00")
    assert str(late) == "Daily 23:00-01:00 (next day) every 40 min"
    whole_day = sched(start="08:00", end="08:00", interval=10)  # end = start: 24 h
    assert whole_day.sprays == 144
    assert sched(start="08:00", end="08:00", interval=40).sprays == 36


def test_overlaps_past_midnight_count_against_the_next_day():
    from godrej.schedule import validate

    friday_night = sched(days=["fri"], start="22:00", end="02:00", interval=20)
    with pytest.raises(ScheduleError, match="overlap"):
        validate([friday_night, sched(days=["sat"], start="01:00", end="03:00", interval=20)])
    validate([friday_night, sched(days=["sat"], start="02:00", end="03:00", interval=20)])
    sunday_night = sched(days=["sun"], start="23:00", end="01:00", interval=20)
    with pytest.raises(ScheduleError, match="overlap"):  # wraps round to Monday
        validate([sunday_night, sched(days=["mon"], start="00:20", end="02:00", interval=20)])


def test_running_schedule_started_yesterday():
    from datetime import datetime
    from godrej.devicestatus import DeviceStatus

    matic = gm.SmartMatic.__new__(gm.SmartMatic)
    matic.schedules = [sched(days=["fri"], start="23:00", end="03:00", interval=20)]
    matic.device_status = DeviceStatus(gm.encode_message(mT=157, mN=157, ack=200, bV=3100, cS=122))
    assert matic.running_schedule(datetime(2026, 10, 31, 1, 30)) == 1  # Saturday 01:30
    assert matic.running_schedule(datetime(2026, 10, 31, 3, 30)) is None


async def test_a_schedule_the_device_drops_is_reported(monkeypatch):
    from godrej.exception import SchedulesNotStored
    from godrej.schedule import Schedule as S

    monkeypatch.setattr(gm, "SCHEDULE_PAUSE", 0)
    device = FakeDevice(command_char=ALL, status_frame=FRAME_308_IN)
    matic = attach(monkeypatch, device)
    too_many = S(("mon",), dtime(23, 0), 40, 250)  # built directly: create() caps it
    with pytest.raises(SchedulesNotStored):
        await matic.set_schedules([too_many])
