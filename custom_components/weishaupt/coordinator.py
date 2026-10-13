"""Data update coordinator for the Weishaupt integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BINARY_PARAMETERS,
    CONF_SCAN_INTERVAL,
    DEFAULT_PATH,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_STALE_UPDATES,
    PARAMETERS,
    REQUEST_TIMEOUT,
    RETRIES,
    RETRY_DELAY,
)
from .protocol import Key, ProtocolError, build_request, parse_response, scale

_LOGGER = logging.getLogger(__name__)

FETCH_ERRORS = (aiohttp.ClientError, TimeoutError, ProtocolError)


async def _fetch_once(
    hass: HomeAssistant, host: str, keys: list[Key]
) -> dict[Key, int | None]:
    session = async_get_clientsession(hass)
    async with session.post(
        f"http://{host}{DEFAULT_PATH}",
        json=build_request(keys),
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
    ) as response:
        response.raise_for_status()
        body = await response.read()
    return parse_response(body.decode("utf-8", errors="replace"), keys)


async def async_fetch(
    hass: HomeAssistant, host: str, keys: list[Key]
) -> dict[Key, int | None]:
    """Ask the device for the given rows, retrying once after a bad answer."""
    for attempt in range(RETRIES + 1):
        try:
            return await _fetch_once(hass, host, keys)
        except FETCH_ERRORS as err:
            if attempt >= RETRIES:
                raise
            _LOGGER.debug("Bad answer from %s (%s), retrying", host, err)
            await asyncio.sleep(RETRY_DELAY)
    raise AssertionError  # pragma: no cover


class WeishauptCoordinator(DataUpdateCoordinator[dict[str, float | None]]):
    """Polls the heating controller and exposes scaled values by key."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(
                seconds=entry.options.get(
                    CONF_SCAN_INTERVAL,
                    entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                )
            ),
        )
        self._host: str = entry.data[CONF_HOST]
        self._failures = 0
        self._keys: list[Key] = [
            (p.module, p.index) for p in (*PARAMETERS, *BINARY_PARAMETERS)
        ]

    async def _async_update_data(self) -> dict[str, float | None]:
        try:
            raw = await async_fetch(self.hass, self._host, self._keys)
        except FETCH_ERRORS as err:
            self._failures += 1
            if self.data is not None and self._failures <= MAX_STALE_UPDATES:
                _LOGGER.debug(
                    "Update %d/%d failed, keeping last values: %s",
                    self._failures,
                    MAX_STALE_UPDATES,
                    err,
                )
                return self.data
            raise UpdateFailed(f"Error talking to {self._host}: {err}") from err

        self._failures = 0
        data = dict(self.data or {})
        for p in PARAMETERS:
            key = (p.module, p.index)
            if key in raw:
                value = raw[key]
                data[p.key] = None if value is None else scale(value, p.factor)
            else:
                # row missing: keep the previous value, unknown if never seen
                data.setdefault(p.key, None)
        for b in BINARY_PARAMETERS:
            key = (b.module, b.index)
            if key in raw:
                value = raw[key]
                data[b.key] = None if value is None else float(value)
            else:
                data.setdefault(b.key, None)
        return data
