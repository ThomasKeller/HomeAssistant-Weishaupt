"""Weishaupt "coco" telegram protocol (no Home Assistant dependencies).

The WCM-COM web server is fragile. Besides valid JSON it was observed to answer
with HTTP 200 and an HTML "server is busy" page, NUL bytes, truncated JSON or a
telegram that belongs to another client (e.g. an open web UI tab). Everything
that is not a clean answer for the requested rows raises a ProtocolError.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

PROTOCOL = "coco"
_INVALID = -32768  # 0x8000: sensor not connected / value not available

# (module type, info number); the same info number exists for several modules
Key = tuple[int, int]


class ProtocolError(ValueError):
    """The device did not return a usable answer."""


class DeviceBusyError(ProtocolError):
    """The device web server reported that it is busy."""


def build_request(keys: Iterable[Key]) -> dict[str, Any]:
    """Build the JSON body asking the device for the given (module, info) pairs."""
    return {
        "prot": PROTOCOL,
        "telegramm": [[module, 0, 1, info, 0, 0, 0, 0] for module, info in keys],
    }


def decode_value(row: list[int]) -> tuple[Key, int | None]:
    """Decode one telegram row to ((module, info), raw signed value).

    The value is a signed 16 bit integer, low byte at [6], high byte at [7].
    0x8000 marks "no value" and is returned as None.
    """
    if len(row) != 8:
        raise ProtocolError(f"unexpected telegram row length {len(row)}")
    raw = row[7] * 256 + row[6]
    if raw >= 0x8000:
        raw -= 0x10000
    return (row[0], row[3]), None if raw == _INVALID else raw


def _load_rows(text: str) -> list[Any]:
    """Clean a response body and return its 'telegramm' list."""
    cleaned = text.replace("\x00", "").strip()
    if not cleaned:
        raise ProtocolError("empty response")
    if "server is busy" in cleaned.lower():
        raise DeviceBusyError("device web server is busy")
    try:
        payload = json.loads(cleaned)
    except ValueError as err:
        raise ProtocolError(f"invalid JSON: {err}") from err
    rows = payload.get("telegramm") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ProtocolError("response has no 'telegramm' list")
    return rows


def parse_counter(text: str, key: Key) -> int:
    """Parse a large (10 byte) telegram holding an unsigned 32 bit counter."""
    for row in _load_rows(text):
        if (
            isinstance(row, list)
            and len(row) == 10
            and all(isinstance(b, int) for b in row)
            and (row[0], row[3]) == key
        ):
            return row[6] | row[7] << 8 | row[8] << 16 | row[9] << 24
    raise ProtocolError(f"response has no counter row for {key}")


def parse_response(text: str, requested: Iterable[Key]) -> dict[Key, int | None]:
    """Parse a response body and return {(module, info): raw value}.

    Rows that were not requested are ignored. Requested rows the device does not
    support are simply absent from the result.
    """
    wanted = set(requested)
    rows = _load_rows(text)

    result: dict[Key, int | None] = {}
    for row in rows:
        if not (
            isinstance(row, list)
            and len(row) == 8
            and all(isinstance(b, int) for b in row)
        ):
            continue  # junk from another client
        key, value = decode_value(row)
        if key in wanted:
            result[key] = value
    if wanted and not result:
        raise ProtocolError("response contains none of the requested rows")
    return result
