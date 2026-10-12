"""Parsing and aggregation of the device data logger CSV (no Home Assistant deps).

The WCM-COM logs 8 configured channels once per minute and serves them as
``GET /x.csv?a=<first>&b=<end>`` (end exclusive), one line per sample::

    12.10.26 22:41:08;18.3;22.4;47.0;9.0;35.0;8.0;9.7;0

The sample count is available as a 32 bit counter (info 20004, module 0).
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

SAMPLE_COUNTER_KEY = (0, 20004)
CSV_PATH = "/x.csv"
CSV_FIELDS = 9  # timestamp + 8 channels

# Channel order of the device logger (as configured in the web UI), mapped to
# the integration's sensor keys.
COLUMNS: tuple[str, ...] = (
    "room_setpoint",  # 416 Soll Temp 1
    "room_temperature",  # 412 Raumtemperatur 1
    "hot_water_temperature",  # I34
    "outside_temperature",  # I33
    "flow_temperature",  # I30 Vorlauftemperatur (eSTB)
    "heat_demand",  # I13
    "damped_outside_temperature",  # I12
    "load_state",  # I11 Laststellung
)

_INVALID_BELOW = -3000.0  # 0x8000 shows up as about -3276.8


class HistoryError(Exception):
    """The logger data could not be read or is not plausible."""


Row = tuple[datetime, tuple["float | None", ...]]  # naive device time, values


def _number(text: str) -> float | None:
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return None if value <= _INVALID_BELOW else value


def parse_csv(text: str) -> list[Row]:
    """Parse logger CSV text; truncated or garbage lines are skipped."""
    rows: list[Row] = []
    for line in re.split(r"[\r\n]+", text.replace("\x00", "")):
        parts = line.strip().split(";")
        if len(parts) != CSV_FIELDS:
            continue
        try:
            stamp = datetime.strptime(parts[0].strip(), "%d.%m.%y %H:%M:%S")
        except ValueError:
            continue
        rows.append((stamp, tuple(_number(p) for p in parts[1:])))
    return rows


class Localizer:
    """Converts naive device time to UTC, resolving the repeated DST hour.

    Samples must be fed in logging order. When the local clock jumps back
    (autumn DST change) the second pass through the hour gets ``fold=1``.
    """

    def __init__(self, tz: tzinfo) -> None:
        self._tz = tz
        self._previous: datetime | None = None

    def to_utc(self, naive: datetime) -> datetime | None:
        """Return the UTC time, or None if it would go back in time."""
        utc = naive.replace(tzinfo=self._tz).astimezone(timezone.utc)
        prev = self._previous
        if prev is not None and utc < prev:
            folded = naive.replace(tzinfo=self._tz, fold=1).astimezone(timezone.utc)
            if folded < prev:
                return None
            utc = folded
        self._previous = utc
        return utc


@dataclass(frozen=True)
class HourStat:
    start: datetime  # UTC, on the hour
    mean: float
    minimum: float
    maximum: float


class HourlyAggregator:
    """Aggregates ordered UTC samples into hourly mean/min/max per column."""

    def __init__(self, columns: int = len(COLUMNS)) -> None:
        self._columns = columns
        self._hour: datetime | None = None
        self._acc: list[list[float]] = []  # per column: [sum, count, min, max]

    @staticmethod
    def hour_of(utc: datetime) -> datetime:
        return utc.replace(minute=0, second=0, microsecond=0)

    def _reset(self, hour: datetime) -> None:
        self._hour = hour
        self._acc = [[0.0, 0, float("inf"), float("-inf")] for _ in range(self._columns)]

    def _finish(self) -> list[tuple[int, HourStat]]:
        assert self._hour is not None
        done = []
        for col, (total, count, low, high) in enumerate(self._acc):
            if count:
                done.append((col, HourStat(self._hour, total / count, low, high)))
        return done

    def add(
        self, utc: datetime, values: tuple[float | None, ...]
    ) -> list[tuple[int, HourStat]]:
        """Add a sample; returns the stats of an hour that this sample completed."""
        hour = self.hour_of(utc)
        done: list[tuple[int, HourStat]] = []
        if self._hour is None:
            self._reset(hour)
        elif hour != self._hour:
            done = self._finish()
            self._reset(hour)
        for col, value in enumerate(values[: self._columns]):
            if value is None:
                continue
            acc = self._acc[col]
            acc[0] += value
            acc[1] += 1
            acc[2] = min(acc[2], value)
            acc[3] = max(acc[3], value)
        return done

    def flush(self) -> list[tuple[int, HourStat]]:
        """Finish the current hour (only call when that hour is complete)."""
        if self._hour is None:
            return []
        done = self._finish()
        self._hour = None
        return done


async def find_first_index_at_or_after(
    count: int,
    target: datetime,
    get_time: Callable[[int], Awaitable[datetime]],
) -> int:
    """Binary search the first sample index whose UTC time is >= target.

    ``get_time(i)`` must return the UTC time of sample ``i`` or raise
    HistoryError. Returns ``count`` if all samples are older than target.
    """
    lo, hi = 0, count
    while lo < hi:
        mid = (lo + hi) // 2
        if await get_time(mid) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo
