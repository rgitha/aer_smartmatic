# Godrej Aer Smart Matic Integration for Home Assistant

A custom integration for the Godrej Aer Smart Matic, a battery-powered
Bluetooth Low Energy (BLE) room freshener. It reads the device's status and
controls it locally over Bluetooth, through an adapter on the Home Assistant
host or an ESPHome Bluetooth proxy. No cloud account is needed.

Forked from [ov1d1u/godrej_aer integration](https://github.com/ov1d1u/godrej_aer).

## Use cases

- Spray when someone enters a room, or before guests arrive.
- Turn auto-spray on while you're home and off when everyone leaves.
- Get notified when the batteries or the refill run low.
- Keep spray schedules on the device, so they run even when Home Assistant
  is down, and edit them from Home Assistant.

## Supported devices

The Godrej Aer Smart Matic. Tested on firmware 3.0.8-IN.

## Supported functions

**Controls**
- **Spray now:** sprays once.
- **Spray: auto:** Off, or every 10, 20 or 40 minutes. The `started_by`
  attribute shows what started it: app, schedule or button.
- **Reset refill level:** after replacing the cartridge.
- **Identify** (diagnostic): lights the device's three interval lights for 3
  seconds. It works only while auto-spray is off and no schedule is running.

**Sensors**
- **Status:** Idle, Auto, Schedule or Disconnected. The mode comes from the
  last status read.
- **Battery:** a percentage of what the device can use, from 3.1 V (100 %)
  down to its 2.4 V cut-off (0 %). The voltage is a diagnostic sensor
  (disabled by default).
- **Refill level** (an estimate from the spray count) and **Spray count**
  (lifetime).
- **Schedules:** the number of schedules on the device. Its attributes list
  them (days, start, end, interval), the one **running** now, and when they
  were **last read**.

**Schedules**

The device runs up to 10 schedules on its own: days, a start and end time,
and an interval of 10, 20 or 40 minutes. An end at or before the start means
the next day, so a window can run past midnight, up to 24 hours. The end is
rounded down to a whole interval, and a schedule holds at most 144 sprays.
To edit them from a dashboard, use the
[Smart Matic card](https://github.com/rgitha/godrej-aer-card).

**Diagnostic sensors**
- **Connection status:** OK, or Unreachable when the device isn't heard or the
  last exchange failed.
- **Updated last:** when the status was last read.
- **Signal strength:** with the adapter or proxy that received it.
- **Channel: command**, **Channel: reply** and **Connection link:** for
  debugging, disabled by default.

**Other**
- **Update status** button and action (also reads the schedules).
- **Diagnostics download:** on the device page, ⋮ → Download diagnostics. The
  address and name are redacted.

## Installation

### HACS

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/rgitha/godrej_aer` as an **Integration**.
3. Install **Godrej Aer Smart Matic** and restart Home Assistant.

### Manual

Copy `custom_components/godrej_aer` into your `config/custom_components`
folder and restart Home Assistant.

## Configuration

Home Assistant discovers devices it can hear and offers them under
**Settings → Devices & services**; choose **Add** and confirm. To add one
yourself, choose **Add integration → Godrej Aer Smart Matic**.

| Step | Parameter | Description |
|---|---|---|
| Select device | Device | A Smart Matic that Home Assistant can hear, or **Enter MAC address manually**. |
| Enter MAC address | MAC address | The device's Bluetooth address, e.g. `AA:BB:CC:DD:EE:FF`. |

Home Assistant then checks that the device responds and adds it as "Smart
Matic". In the dialog that follows, give it a name (e.g. Hall) and an area.

There are no other options. If the device stops answering after a firmware
update, use **Reconfigure** on the integration entry.

## Removal

1. Go to **Settings → Devices & services → Godrej Aer Smart Matic**.
2. Open the device entry's ⋮ menu and choose **Delete**.
3. To remove the integration completely, uninstall it in HACS (or delete
   `custom_components/godrej_aer`) and restart Home Assistant.

The device keeps its auto-spray setting and schedules, and runs on its own.

## Data updates

The device doesn't push changes, so the integration reads its status:
- once an hour while the device is heard;
- after every command;
- when you press **Update status** or call the action.

The schedules are read once after startup, after every schedule change and
with Update status.

To save battery, it disconnects after each exchange, so the link opening and
closing is normal. After a failed read it retries after 30 s, 1, 2 and 5
minutes, then every 5 minutes until the device answers.

When Home Assistant hasn't heard the device's advertisements for a few minutes
(typically 3 to 8), Connection status turns Unreachable, Status shows
Disconnected, and the readings and controls become unavailable. They return
as soon as the device is heard again.

## Actions

### `godrej_aer.update_status`

Reads the status and schedules now, ignoring the retry wait. Target one or
more devices or entities, or leave the target out to update every device.

```yaml
action: godrej_aer.update_status
target:
  device_id: <your Smart Matic device>
```

### Schedule actions

All target one or more devices.

| Action | Fields |
|---|---|
| `godrej_aer.set_schedules` | `schedules`: the whole list. Optional `expected`: the list you started from; nothing is written if the device's list changed meanwhile. |
| `godrej_aer.add_schedule` | `days`, `start`, `end`, `interval` |
| `godrej_aer.remove_schedule` | `number`: its number in the Schedules sensor's list |
| `godrej_aer.clear_schedules` | none |
| `godrej_aer.stop_schedule` | `schedule`: `current`, `all` or a number. Skips a running schedule; the device acknowledges but ignores a number that isn't running. |

Every upload is read back, and an error is shown if the device didn't keep it.
Saving schedules interrupts a running schedule, as the device replaces the
whole list. A saved schedule covering the current time starts within about a
minute, at the device's next schedule check, with a spray; its sprays then
follow from that moment.

```yaml
action: godrej_aer.add_schedule
target:
  device_id: <your Smart Matic device>
data:
  days: [mon, tue, wed, thu, fri]
  start: "07:00"
  end: "10:00"
  interval: 20
```

## Examples

Spray when motion is detected in the hall, at most once every 15 minutes:

```yaml
automation:
  - alias: Hall freshener on motion
    mode: single
    triggers:
      - trigger: state
        entity_id: binary_sensor.hall_motion
        to: "on"
    actions:
      - action: button.press
        target:
          entity_id: button.hall_spray_now
      - delay: "00:15:00"
```

Notify when the battery is low:

```yaml
automation:
  - alias: Hall freshener battery low
    triggers:
      - trigger: numeric_state
        entity_id: sensor.hall_battery
        below: 15
    actions:
      - action: notify.notify
        data:
          message: "Replace the batteries in the hall freshener."
```

## Known limitations

- **Range:** the device's Bluetooth range is short. Keep it close to an
  adapter or proxy.
- **One connection at a time:** while a phone is connected to the device, Home
  Assistant can't reach it.
- **Status isn't live:** changes made with the device's button or a phone show
  up at the next status read (up to an hour), or sooner with Update status.
- **Low batteries:** below 2.4 V the device still acknowledges sprays without
  spraying. Spray now and Spray: auto then show "Batteries are too low to
  spray".
- **Refill level** is an estimate from the spray count, not a measurement.
- **Schedules from other tools:** a phone app that writes the device's
  schedules may not read them first, so a change made there can replace the
  ones set here. Manage them from one place.
- **Skipping ahead:** only a schedule that is running can be skipped; a later
  run can't be skipped in advance.
- **Saving interrupts a run:** changing any schedule while one is running
  interrupts it for about a minute; it then starts again with a spray.

## Troubleshooting

- **Device not found, or "The device didn't respond":** check that it has
  batteries, move it closer to an adapter or proxy, disconnect any phone
  connected to it, and try again.
- **Unreachable or Disconnected:** as above. **Signal strength** shows how well
  the adapter or proxy hears the device.
- **A command shows an error:** the device didn't confirm it within 5 seconds.
  The status is read back afterwards, so the entities show what the device is
  actually doing.
- **The proxy seems stuck:** an ESPHome Bluetooth proxy can get into a state
  where it still hears the device but can't connect to it; every attempt
  times out. The log then warns that the device "is heard but can't be
  connected to". Restart the proxy.
- **Reporting a problem:** attach the diagnostics download, and a debug log
  (on the integration page, ⋮ → Enable debug logging).

## Development

The device library has a test suite that runs without Home Assistant:

```
pip install pytest pytest-asyncio bleak bleak-retry-connector
pytest
```

The config flow, entities and entry setup are tested against Home Assistant
itself, in a separate environment:

```
pip install pytest-homeassistant-custom-component
pytest tests_ha
```

The code is fully typed; check it in the Home Assistant environment with:

```
pip install mypy
mypy --strict custom_components/godrej_aer
```

## License

Apache License 2.0.

## Contributions

Bug reports and suggestions are welcome as GitHub issues, and fixes as pull
requests.

## Disclaimer

This project is not associated with Godrej Aer. All rights regarding the
devices are reserved to Godrej Aer. It is experimental and not officially
supported. Use at your own risk.
