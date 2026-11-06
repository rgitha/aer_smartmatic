from homeassistant.exceptions import HomeAssistantError


class InvalidDeviceError(HomeAssistantError):
    pass

class NotConnectedError(HomeAssistantError):
    pass

class ConnectionError(HomeAssistantError):
    pass

class DeviceBusy(ConnectionError):
    """A user's request didn't get its turn in time: another exchange
    with the device was still running."""

class CommandNotConfirmed(HomeAssistantError):
    """The device did not acknowledge a command (or rejected it)."""

class NotIdle(HomeAssistantError):
    """Identify needs the device idle: no auto-spray, no schedule run.
    ``schedule`` says a schedule run is what keeps it busy."""

    def __init__(self, message: str, *, schedule: bool = False):
        super().__init__(message)
        self.schedule = schedule

class ScheduleRunning(HomeAssistantError):
    """Auto can't be changed while a schedule runs (the device ignores it)."""


class SchedulesChanged(HomeAssistantError):
    """The device's schedules differ from the list an edit started from."""

class SchedulesNotStored(HomeAssistantError):
    """The device acknowledged a schedule upload but doesn't hold the list."""

class BatteryTooLow(HomeAssistantError):
    """A spray command was acknowledged, but the batteries are below the
    device's cut-off, so the motor won't run."""


class NoClock(HomeAssistantError):
    """The device has no working clock (found at setup), so it can't keep
    schedules: they aren't read or saved."""
