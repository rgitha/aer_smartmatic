"""The device's own spray schedules (message types 103, 108 and 109).

* The device holds a list of up to 10 schedules, all active at once.
* Each is 5 bytes: a day bitmask (Mon = 64, Tue = 32, ... Sun = 1), the
  start hour, the start minute, the number of sprays in the window, and
  the interval in minutes. There is no end time: it is start + sprays x
  interval, which may run past midnight. At most 144 sprays are stored.
* The whole list is replaced at once: "remove all" (108), a pause, then
  every schedule in one "set schedules" message (103), written to
  6e400004. "Retrieve" (109) returns the same 5-byte entries.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from .const import SPRAY_INTERVALS

# Day order, and each day's bit in the mask.
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_BITS = {day: 1 << (6 - i) for i, day in enumerate(DAYS)}
_DAY_LABELS = {
    "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}

MAX_SCHEDULES = 10
# The most sprays the device stores in one schedule (fw 3.0.8-IN, at any
# interval). Above this it acknowledges the upload but drops the schedule.
MAX_SPRAYS = 144
_MINUTES_PER_DAY = 24 * 60
_MINUTES_PER_WEEK = 7 * _MINUTES_PER_DAY


class ScheduleError(ValueError):
    """A schedule the device can't hold, with a message for the user."""


@dataclass(frozen=True)
class Schedule:
    days: tuple[str, ...]  # in DAYS order
    start: time
    interval: int  # minutes
    sprays: int  # sprays in the window

    @classmethod
    def create(
        cls, days: str | Iterable[str], start: time, end: time, interval: int
    ) -> "Schedule":
        """Build a schedule from a start and end time, checking it fits.

        An end at or before the start means the next day (the device runs a
        window across midnight as one schedule), so a window is at most 24
        hours: it never reaches its own next start. The end is rounded down
        to the last whole interval, as the device only stores the number of
        sprays.
        """
        day_list = _normalise_days(days)
        if interval not in SPRAY_INTERVALS:
            raise ScheduleError(
                f"Interval must be one of {', '.join(map(str, SPRAY_INTERVALS))} minutes"
            )
        start_min = start.hour * 60 + start.minute
        end_min = end.hour * 60 + end.minute
        if end_min <= start_min:
            end_min += _MINUTES_PER_DAY  # ends the next day
        sprays = (end_min - start_min) // interval
        if sprays < 1:
            raise ScheduleError(
                f"The window {start:%H:%M}-{end:%H:%M} is shorter than one "
                f"{interval}-minute interval"
            )
        return cls(day_list, time(start.hour, start.minute), interval, min(sprays, MAX_SPRAYS))

    @classmethod
    def from_bytes(cls, data: bytes) -> "Schedule":
        if len(data) != 5:
            raise ScheduleError(f"Schedule entry has {len(data)} bytes, expected 5")
        mask, hour, minute, sprays, interval = data
        days = tuple(day for day in DAYS if mask & _DAY_BITS[day])
        return cls(days, time(hour % 24, minute % 60), interval, sprays)

    def to_bytes(self) -> bytes:
        mask = 0
        for day in self.days:
            mask |= _DAY_BITS[day]
        return bytes([mask, self.start.hour, self.start.minute, self.sprays, self.interval])

    @property
    def start_minutes(self) -> int:
        return self.start.hour * 60 + self.start.minute

    @property
    def end_minutes(self) -> int:
        return self.start_minutes + self.sprays * self.interval

    @property
    def end(self) -> time:
        minutes = self.end_minutes % _MINUTES_PER_DAY
        return time(minutes // 60, minutes % 60)

    @property
    def next_day(self) -> bool:
        """True when the window ends after midnight."""
        return self.end_minutes > _MINUTES_PER_DAY

    def windows(self) -> list[tuple[int, int]]:
        """The schedule's windows as (start, end) minutes from Monday 00:00;
        one past the end of the week wraps round to Monday."""
        out = []
        for day in self.days:
            start = DAYS.index(day) * _MINUTES_PER_DAY + self.start_minutes
            end = start + self.sprays * self.interval
            out.append((start, min(end, _MINUTES_PER_WEEK)))
            if end > _MINUTES_PER_WEEK:
                out.append((0, end - _MINUTES_PER_WEEK))
        return out

    def as_dict(self) -> dict[str, Any]:
        """For entity attributes and logs."""
        return {
            "days": [_DAY_LABELS[d] for d in self.days],
            "start": f"{self.start:%H:%M}",
            "end": f"{self.end:%H:%M}",
            "next_day": self.next_day,
            "interval": self.interval,
        }

    def __str__(self) -> str:
        return (
            f"{_days_text(self.days)} {self.start:%H:%M}-{self.end:%H:%M}"
            f"{' (next day)' if self.next_day else ''} every {self.interval} min"
        )


def validate(schedules: list[Schedule]) -> None:
    """Check a whole list against the device's limits.

    Overlapping windows are refused: what the device would do with them
    isn't known. A window past midnight counts against the
    next day's windows too (Sunday's against Monday's).
    """
    if len(schedules) > MAX_SCHEDULES:
        raise ScheduleError(f"The device holds at most {MAX_SCHEDULES} schedules")
    for i, a in enumerate(schedules):
        for b in schedules[i + 1:]:
            if any(
                a_start < b_end and b_start < a_end
                for a_start, a_end in a.windows()
                for b_start, b_end in b.windows()
            ):
                raise ScheduleError(f"Schedules overlap: {a} and {b}")


def normalise_schedule_dict(item: dict[str, Any]) -> dict[str, Any]:
    """A schedule as given back by a caller (e.g. from the Schedules
    sensor's attributes) in ``Schedule.as_dict()`` form, for comparing."""
    return {
        "days": [_DAY_LABELS[d] for d in _normalise_days(item.get("days", []))],
        "start": str(item.get("start", ""))[:5],
        "end": str(item.get("end", ""))[:5],
        "interval": int(item.get("interval", 0)),
    }


def _normalise_days(days: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(days, str):
        days = [days]
    chosen: set[str] = set()
    for day in days:
        key = str(day).strip().lower()
        if key in ("daily", "all", "every day"):
            chosen.update(DAYS)
        elif key[:3] in _DAY_BITS:
            chosen.add(key[:3])
        else:
            raise ScheduleError(f"Unknown day: {day}")
    if not chosen:
        raise ScheduleError("Pick at least one day")
    return tuple(day for day in DAYS if day in chosen)


def _days_text(days: tuple[str, ...]) -> str:
    if len(days) == 7:
        return "Daily"
    return ", ".join(_DAY_LABELS[d] for d in days)


def next_change(
    schedules: list[Schedule], now: datetime
) -> tuple[datetime, bool] | None:
    """When the next schedule starts or ends after ``now``, and whether it
    starts (True) or ends. A window that runs past midnight ends the next
    day; if one schedule ends as another starts, that counts as a start.
    """
    changes: dict[int, bool] = {}
    for schedule in schedules:
        for day in schedule.days:
            start = (DAYS.index(day) * _MINUTES_PER_DAY + schedule.start_minutes) % _MINUTES_PER_WEEK
            end = (start + schedule.sprays * schedule.interval) % _MINUTES_PER_WEEK
            changes[start] = True
            changes.setdefault(end, False)
    if not changes:
        return None
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    elapsed = (now - week_start).total_seconds() / 60
    best: tuple[float, bool] | None = None
    for minute, starting in changes.items():
        ahead = (minute - elapsed) % _MINUTES_PER_WEEK or _MINUTES_PER_WEEK
        if best is None or ahead < best[0]:
            best = (ahead, starting)
    assert best is not None
    return now + timedelta(minutes=best[0]), best[1]
