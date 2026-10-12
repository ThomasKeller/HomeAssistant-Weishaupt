"""Services of the Weishaupt integration: one-off import of the device history."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.const import CONF_HOST, UnitOfTemperature
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import DEFAULT_PATH, DOMAIN, PARAMETERS, REQUEST_TIMEOUT
from .coordinator import FETCH_ERRORS
from .history import (
    COLUMNS,
    CSV_PATH,
    SAMPLE_COUNTER_KEY,
    HistoryError,
    HourlyAggregator,
    HourStat,
    Localizer,
    find_first_index_at_or_after,
    parse_csv,
)
from .protocol import build_request, parse_counter

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_HISTORY = "import_history"
ATTR_MONTHS = "months"
ATTR_ENTRY = "config_entry_id"

BLOCK_SIZE = 1000  # samples per CSV request (about 7 s on the device)
BLOCK_PAUSE = 2  # seconds between CSV requests
CSV_TIMEOUT = 90  # seconds
FLUSH_HOURS = 24 * 7
MAX_CLOCK_DEVIATION = timedelta(minutes=15)
LIVE_TOLERANCE = 1.5  # K, last logger sample vs. live value
LIVE_CHECK_KEYS = ("outside_temperature", "room_temperature", "damped_outside_temperature")
DAYS_PER_MONTH = 30.4375

_RUNNING = "import_running"
_ERRORS = (*FETCH_ERRORS, HistoryError)

IMPORT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_MONTHS, default=12): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=36)
        ),
        vol.Optional(ATTR_ENTRY): cv.string,
    }
)


@dataclass
class _Plan:
    host: str
    count: int
    tz: tzinfo
    until: datetime  # UTC, on the hour; hours at or after this are not imported
    start: datetime  # UTC
    targets: dict[int, tuple[str, str | None]]  # column -> (entity_id, unit)


# ------------------------------------------------------------------ device access


async def _retry(factory, attempts: int = 4):
    for attempt in range(attempts):
        try:
            return await factory()
        except _ERRORS as err:
            if attempt == attempts - 1:
                raise
            delay = 4 * (attempt + 1)
            _LOGGER.debug("Device request failed (%s), retry in %ss", err, delay)
            await asyncio.sleep(delay)
    raise AssertionError  # pragma: no cover


async def _read_count(hass: HomeAssistant, host: str) -> int:
    session = async_get_clientsession(hass)
    async with session.post(
        f"http://{host}{DEFAULT_PATH}",
        json=build_request([SAMPLE_COUNTER_KEY]),
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
    ) as response:
        response.raise_for_status()
        body = await response.read()
    return parse_counter(body.decode("utf-8", errors="replace"), SAMPLE_COUNTER_KEY)


async def _get_csv(hass: HomeAssistant, host: str, first: int, end: int):
    session = async_get_clientsession(hass)
    async with session.get(
        f"http://{host}{CSV_PATH}",
        params={"a": first, "b": end},
        timeout=aiohttp.ClientTimeout(total=CSV_TIMEOUT),
    ) as response:
        response.raise_for_status()
        body = await response.read()
    rows = parse_csv(body.decode("utf-8", errors="replace"))
    if not rows:
        raise HistoryError(f"no usable samples in {first}..{end}")
    return rows


# ------------------------------------------------------------------ statistics


def _statistic_metadata(entity_id: str, unit: str | None) -> dict[str, Any]:
    """Build recorder metadata for the fields this HA version knows."""
    from homeassistant.components.recorder.models import StatisticMetaData

    fields = set(getattr(StatisticMetaData, "__annotations__", {}))
    meta: dict[str, Any] = {
        "source": "recorder",
        "statistic_id": entity_id,
        "name": None,
        "unit_of_measurement": unit,
        "has_sum": False,
    }
    if "mean_type" in fields:
        from homeassistant.components.recorder.models import StatisticMeanType

        meta["mean_type"] = StatisticMeanType.ARITHMETIC
    if "has_mean" in fields:
        meta["has_mean"] = True
    if "unit_class" in fields:
        from homeassistant.util.unit_conversion import TemperatureConverter

        meta["unit_class"] = (
            getattr(TemperatureConverter, "UNIT_CLASS", "temperature")
            if unit == UnitOfTemperature.CELSIUS
            else None
        )
    return meta


def _statistic_data(stat: HourStat) -> dict[str, Any]:
    return {
        "start": stat.start,
        "mean": round(stat.mean, 3),
        "min": stat.minimum,
        "max": stat.maximum,
    }


# ------------------------------------------------------------------ service


def _units_by_key() -> dict[str, str | None]:
    return {p.key: p.unit for p in PARAMETERS}


def _resolve_entry(hass: HomeAssistant, entry_id: str | None):
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if getattr(e, "runtime_data", None) is not None
    ]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if len(entries) != 1:
        raise ServiceValidationError(
            "Exactly one loaded Weishaupt config entry is required; "
            f"found {len(entries)} (use {ATTR_ENTRY} to choose)."
        )
    return entries[0]


def _resolve_targets(hass: HomeAssistant, entry) -> dict[int, tuple[str, str | None]]:
    registry = er.async_get(hass)
    units = _units_by_key()
    targets: dict[int, tuple[str, str | None]] = {}
    for col, key in enumerate(COLUMNS):
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{key}"
        )
        if entity_id is None:
            _LOGGER.warning("No entity for logger column %s, skipped", key)
            continue
        targets[col] = (entity_id, units.get(key))
    if not targets:
        raise ServiceValidationError("No Weishaupt sensor entities found.")
    return targets


async def _preflight(hass: HomeAssistant, entry, months: int) -> _Plan:
    host: str = entry.data[CONF_HOST]
    tz = dt_util.get_time_zone(hass.config.time_zone) or dt_util.DEFAULT_TIME_ZONE
    try:
        count = await _retry(lambda: _read_count(hass, host))
        if count < 10:
            raise HistoryError(f"logger holds only {count} samples")
        last_rows = await _retry(lambda: _get_csv(hass, host, count - 3, count))
    except _ERRORS as err:
        raise ServiceValidationError(f"Cannot read the device logger: {err}") from err

    naive, values = last_rows[-1]
    last_utc = Localizer(tz).to_utc(naive)
    now = dt_util.utcnow()
    if last_utc is None or abs(now - last_utc) > MAX_CLOCK_DEVIATION:
        raise ServiceValidationError(
            f"Last logger sample ({naive:%d.%m.%y %H:%M:%S}) deviates more than "
            f"{int(MAX_CLOCK_DEVIATION.total_seconds() // 60)} min from the current "
            f"time in {tz}. Device clock or time zone does not match; aborting."
        )

    live = entry.runtime_data.data or {}
    compared = 0
    for key in LIVE_CHECK_KEYS:
        logged = values[COLUMNS.index(key)]
        current = live.get(key)
        if logged is None or current is None:
            continue
        compared += 1
        if abs(logged - current) > LIVE_TOLERANCE:
            raise ServiceValidationError(
                f"Logger channel '{key}' ({logged}) does not match the live value "
                f"({current}); the logger channel configuration probably differs. "
                "Aborting."
            )
    if compared < 2:
        raise ServiceValidationError(
            "Cannot verify the logger channel mapping against live values; try again "
            "when the sensors have values."
        )

    created = getattr(entry, "created_at", None) or now
    until = HourlyAggregator.hour_of(min(now, created))
    start = until - timedelta(days=round(months * DAYS_PER_MONTH))
    return _Plan(host, count, tz, until, start, _resolve_targets(hass, entry))


async def _import(hass: HomeAssistant, plan: _Plan) -> tuple[int, int]:
    """Download the logger CSV in blocks and import hourly statistics.

    Returns (hours per entity imported, samples read).
    """

    async def sample_time(index: int) -> datetime:
        rows = await _retry(
            lambda: _get_csv(hass, plan.host, index, min(index + 3, plan.count))
        )
        utc = Localizer(plan.tz).to_utc(rows[0][0])
        if utc is None:
            raise HistoryError(f"unusable timestamp at sample {index}")
        return utc

    index = await find_first_index_at_or_after(plan.count, plan.start, sample_time)
    _LOGGER.info(
        "Importing Weishaupt history from sample %d of %d (%s .. %s)",
        index,
        plan.count,
        plan.start,
        plan.until,
    )

    localizer = Localizer(plan.tz)
    aggregator = HourlyAggregator()
    pending: dict[int, list[dict[str, Any]]] = {c: [] for c in plan.targets}
    hours = samples = blocks = 0
    reached_until = False

    def collect(done: list[tuple[int, HourStat]]) -> None:
        for col, stat in done:
            if col in pending:
                pending[col].append(_statistic_data(stat))

    first_col = min(pending)

    def flush(force: bool = False) -> None:
        nonlocal hours
        for col, data in pending.items():
            if data and (force or len(data) >= FLUSH_HOURS):
                entity_id, unit = plan.targets[col]
                async_import_statistics(
                    hass, _statistic_metadata(entity_id, unit), list(data)
                )
                if col == first_col:
                    hours += len(data)
                data.clear()

    while index < plan.count and not reached_until:
        end = min(index + BLOCK_SIZE, plan.count)
        rows = await _retry(lambda: _get_csv(hass, plan.host, index, end))
        for naive, values in rows:
            utc = localizer.to_utc(naive)
            if utc is None:
                continue
            if utc >= plan.until:
                reached_until = True
                break
            samples += 1
            collect(aggregator.add(utc, values))
        index = end
        blocks += 1
        flush()
        if blocks % 25 == 0:
            _LOGGER.info("Weishaupt history import: sample %d of %d", index, plan.count)
        await asyncio.sleep(BLOCK_PAUSE)

    if reached_until:
        collect(aggregator.flush())
    flush(force=True)
    return hours, samples


async def _run_import(hass: HomeAssistant, plan: _Plan) -> None:
    notification = f"{DOMAIN}_import_history"
    try:
        hours, samples = await _import(hass, plan)
    except asyncio.CancelledError:
        raise
    except (*_ERRORS, HomeAssistantError, ValueError) as err:
        _LOGGER.error("Weishaupt history import failed: %s", err)
        persistent_notification.async_create(
            hass,
            f"Der Import der Weishaupt-Historie ist fehlgeschlagen: {err}\n\n"
            "Bereits importierte Stunden bleiben erhalten; der Import kann "
            "wiederholt werden.",
            title="Weishaupt Historie-Import",
            notification_id=notification,
        )
    else:
        persistent_notification.async_create(
            hass,
            f"Import abgeschlossen: {samples} Samples gelesen, {hours} Stunden "
            f"je Sensor bis {plan.until:%d.%m.%Y %H:%M} UTC importiert.",
            title="Weishaupt Historie-Import",
            notification_id=notification,
        )
    finally:
        hass.data[DOMAIN].pop(_RUNNING, None)


async def _handle_import(hass: HomeAssistant, call: ServiceCall) -> None:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_RUNNING):
        raise ServiceValidationError("A history import is already running.")
    entry = _resolve_entry(hass, call.data.get(ATTR_ENTRY))
    domain_data[_RUNNING] = True
    try:
        plan = await _preflight(hass, entry, call.data[ATTR_MONTHS])
    except BaseException:
        domain_data.pop(_RUNNING, None)
        raise
    entry.async_create_background_task(
        hass, _run_import(hass, plan), name="weishaupt_import_history"
    )
    _LOGGER.info(
        "Weishaupt history import started: %d months, up to %s",
        call.data[ATTR_MONTHS],
        plan.until,
    )


def async_register_services(hass: HomeAssistant) -> None:
    async def handler(call: ServiceCall) -> None:
        await _handle_import(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_HISTORY, handler, schema=IMPORT_SCHEMA
    )
