"""Device- and BLE-connection constants for the Aer Smart Matic."""

# Nordic UART-style GATT service and characteristics exposed by the device.
MAIN_SVC = "6e400000-b5a3-f393-e0a9-e50e24dcca9e"
# Replies arrive here on all firmware seen so far (the vendor app's
# RESPONSE characteristic). It has no CCCD; see _raw_proxy_notify.
NOTIFY_CHAR = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

# Default channels: commands on 6e400003, replies on NOTIFY_CHAR
# (6e400002 takes only the provisioning message).
# Fw 3.0.8-IN accepts commands on 6e400002, 6e400003 and 6e400004 alike.
# Setup checks the defaults and only falls back to probing every writable
# characteristic (these first) if they don't answer.
COMMAND_CHAR_CANDIDATES = (
    "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
    "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
    "6e400004-b5a3-f393-e0a9-e50e24dcca9e",
)
DEFAULT_COMMAND_CHAR = COMMAND_CHAR_CANDIDATES[0]
# Schedule uploads (message type 103) go to this characteristic.
SCHEDULE_CHAR = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
DEFAULT_REPLY_CHAR = NOTIFY_CHAR

# Seconds a link stays open after the last exchange, for a quick second
# press (keeping it open drains the device battery). Multi-step operations (a schedule save, Identify) hold it open
# until they finish.
DISCONNECT_DELAY = 5
# Seconds to wait for the status notification after asking for it. Replies
# usually take well under a second; the slowest seen took about 3 s.
STATUS_TIMEOUT = 5
# Seconds to wait for a reply from each candidate while probing (replies
# have been seen to take up to ~3 s).
PROBE_TIMEOUT = 8
# Total time detection may take (defaults check plus fallback probing).
DETECT_TIMEOUT = 60
# Schedules: seconds to wait for their replies, and the pause left between
# "remove all" and the upload that follows it.
SCHEDULE_REPLY_TIMEOUT = 5
SCHEDULE_PAUSE = 3
# Seconds to wait for a command's acknowledgement, counted from its write.
# Fresh links answer in ~0.1 s; on a link left open for a few seconds the
# device has been seen to take just over 3 s.
ACK_TIMEOUT = 5
# One connection attempt runs at a time, and the integration never cuts it
# short: bleak-retry-connector's own timeout ends a failing one. Someone
# waiting (a press, Update status) gets an answer after CONNECT_TIMEOUT
# seconds; the attempt carries on, a press meanwhile joins it, and a link
# that comes up after everyone stopped waiting is closed. A working device
# connects in about 1 s.
CONNECT_ATTEMPTS = 1
CONNECT_TIMEOUT = 5
# Background reads, which nobody is waiting on, wait for the whole attempt.
BACKGROUND_PURPOSES = ("status poll", "read-back", "clock check")
# Seconds after a save to read the status, when a saved schedule covers the
# current time: the device starts it at its next schedule check (about
# every 45-50 s), with a spray. Reads stop once the status shows the run.
SCHEDULE_START_READS = (30, 60, 90)
# After this many failed attempts in a row while the device is still
# heard, warn that the adapter or proxy may need a restart.
STUCK_AFTER = 3
# A failed attempt counts towards that only if the device advertised within
# this many seconds: Home Assistant takes minutes to notice a device that
# went quiet (batteries out, moved away), and that isn't the proxy's fault.
HEARD_RECENTLY = 30
# On each new link the device's clock is read (114 -> 164) and set (102)
# only if it is more than this many seconds off, or can't be read.
CLOCK_TOLERANCE = 10
# In use, the clock is checked by background exchanges only, never ahead of
# an action someone is waiting for: at most once an hour, and also after
# Home Assistant starts, when the device is heard again (its batteries may
# have been out), at each schedule start and end, and before a save.
CLOCK_CHECK_INTERVAL = 3600
# New batteries (their clock started again): the level rose by more than
# this many points between two status reads, to at least NEW_BATTERY_MIN
# (the same rule as Spritz). The clock is then checked at once.
NEW_BATTERY_RISE = 10
NEW_BATTERY_MIN = 90
CLOCK_READ_TIMEOUT = 2
# Setup's clock check: tries of each step, and seconds to wait for each.
CLOCK_CHECK_ATTEMPTS = 2
CLOCK_CHECK_TIMEOUT = 5
# On unload, how long to let a schedule save in progress finish.
SAVE_FINISH_TIMEOUT = 30
# Seconds to wait after connecting before the first message, for every
# exchange (commands and background reads alike). Setup waits SETUP_SETTLE,
# as it may meet firmware not seen before.
ACTION_SETTLE = 0.5
SETUP_SETTLE = 2.0
# Seconds to back off background polls after consecutive failed attempts:
# short at first (e.g. a proxy still reconnecting after a restart), then
# every 5 minutes while the device stays unreachable. A success resets it.
CONNECT_RETRY_BACKOFF = (30, 60, 120, 300)

# Auto-spray intervals the device supports, in minutes.
SPRAY_INTERVALS = (10, 20, 40)
# Interval used when auto-spray is switched on without an explicit choice
# (the device's own default).
DEFAULT_SPRAY_INTERVAL = 10
# The device reports the open-circuit voltage of its two AA cells and
# stops driving the motor below 2.4 V (it still acknowledges sprays).
BATTERY_CUTOFF_MV = 2400
# (mV, %) points of the battery level, interpolated linearly in between:
# an alkaline cell's light-load discharge curve (a fit to datasheet points
# at 1 mA, per cell, doubled), rescaled so the cut-off is 0 %.
BATTERY_CURVE = (
    (3100, 100),
    (3000, 95),
    (2900, 91),
    (2800, 86),
    (2700, 78),
    (2600, 57),
    (2500, 29),
    (2450, 14),
    (BATTERY_CUTOFF_MV, 0),
)
# Sprays a full cartridge is rated for; used to derive a refill percentage.
SPRAY_CAPACITY = 2200
