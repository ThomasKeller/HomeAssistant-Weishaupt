#!/usr/bin/env python3
"""Download the data logger history of a Weishaupt WCM-COM to a local file.

The WCM-COM logs 8 channels once per minute and serves them as
``GET /x.csv?a=<first>&b=<end>`` (end exclusive). Its web server is slow
(about 150 samples/s) and drops requests when it is busy, so this tool reads
in blocks, pauses between blocks, retries with backoff and can be resumed:
run it again with the same ``--out`` file and it continues where it stopped.

The output file contains exactly one logger sample per line; the sample index
of the first line is stored in ``<out>.meta.json``.

    python download_log.py --host 192.168.111.31 --out weishaupt_log.csv

Afterwards serve the file with ``serve_log.py`` and import it into Home
Assistant with ``weishaupt.import_history`` (parameter ``source``).

Only the Python standard library is needed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

COUNTER_INFO = 20004  # sample counter (unsigned 32 bit), module 0
# one sample: timestamp and 8 channel values; a value may be empty (no reading)
LINE = re.compile(
    r"^\d{2}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}(;(?:-?\d+(?:[.,]\d+)?)?){8}$"
)


class DeviceError(Exception):
    """The device did not return a usable answer."""


def _get(url: str, data: bytes | None, timeout: float) -> str:
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data, headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
    return text.replace("\x00", "")


def read_counter(base: str, timeout: float = 15) -> int:
    """Return the number of samples the logger holds (= next sample index)."""
    body = json.dumps(
        {"prot": "coco", "telegramm": [[0, 0, 1, COUNTER_INFO, 0, 0, 0, 0]]}
    ).encode()
    try:
        text = _get(f"{base}/parameter.json", body, timeout).strip()
    except OSError as err:
        raise DeviceError(f"counter request failed: {err}") from err
    if "server is busy" in text.lower():
        raise DeviceError("device web server is busy")
    try:
        rows = json.loads(text)["telegramm"]
    except (ValueError, KeyError, TypeError) as err:
        raise DeviceError(f"invalid counter response: {text[:80]!r}") from err
    for row in rows:
        if isinstance(row, list) and len(row) == 10 and row[3] == COUNTER_INFO:
            return row[6] | row[7] << 8 | row[8] << 16 | row[9] << 24
    raise DeviceError("counter row missing")


def fetch_block(base: str, first: int, end: int, timeout: float = 90) -> list[str]:
    """Return exactly ``end - first`` valid sample lines or raise DeviceError."""
    try:
        text = _get(f"{base}/x.csv?a={first}&b={end}", None, timeout)
    except OSError as err:
        raise DeviceError(f"block {first}..{end} failed: {err}") from err
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
    invalid = sum(1 for ln in lines if not LINE.match(ln))
    if invalid or len(lines) != end - first:
        hint = " (device busy)" if "busy" in text.lower() else ""
        raise DeviceError(
            f"block {first}..{end}: expected {end - first} lines, got "
            f"{len(lines)} with {invalid} invalid{hint}"
        )
    return lines


def _count_lines(path: str) -> int:
    count = 0
    with open(path, "rb") as handle:
        for _ in handle:
            count += 1
    return count


def download(
    base: str,
    out: str,
    block: int = 1000,
    pause: float = 2.0,
    max_failures: int = 8,
    start: int = 0,
    limit: int | None = None,
    log=print,
) -> int:
    """Download samples into ``out``; returns the next sample index."""
    meta_path = out + ".meta.json"
    first = start
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as handle:
            first = json.load(handle)["first_index"]
    existing = _count_lines(out) if os.path.exists(out) else 0
    if existing == 0:
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump({"first_index": first}, handle)
    next_index = first + existing
    log(f"first sample index {first}, {existing} samples already in {out}")

    def attempt(func):
        failures = 0
        while True:
            try:
                return func()
            except DeviceError as err:
                failures += 1
                if failures >= max_failures:
                    raise
                delay = min(5 * 2 ** (failures - 1), 120)
                log(f"  {err}; retry {failures}/{max_failures - 1} in {delay}s")
                time.sleep(delay)

    count = attempt(lambda: read_counter(base))
    stop_at = min(count, first + limit) if limit is not None else count
    log(f"logger holds {count} samples; downloading up to index {stop_at}")
    started, done = time.time(), 0
    while True:
        while next_index < stop_at:
            end = min(next_index + block, stop_at)
            lines = attempt(lambda: fetch_block(base, next_index, end))
            with open(out, "a", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines) + "\n")
            done += end - next_index
            next_index = end
            rate = done / max(time.time() - started, 1e-6)
            eta = (stop_at - next_index) / rate if rate else 0
            log(
                f"  {next_index}/{stop_at} ({100 * (next_index - first) / max(stop_at - first, 1):.1f}%) "
                f"{rate:.0f} samples/s, ETA {eta / 60:.0f} min"
            )
            time.sleep(pause)
        if limit is not None:
            break
        new_count = attempt(lambda: read_counter(base))  # catch up with new samples
        if new_count <= stop_at:
            break
        count = stop_at = new_count
    log(f"done: {next_index - first} samples in {out}")
    return next_index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", required=True, help="device address, e.g. 192.168.111.31")
    parser.add_argument("--out", default="weishaupt_log.csv", help="output file (resumable)")
    parser.add_argument("--block", type=int, default=1000, help="samples per request")
    parser.add_argument("--pause", type=float, default=2.0, help="seconds between requests")
    parser.add_argument("--max-failures", type=int, default=8, help="give up after N failures in a row")
    parser.add_argument("--start", type=int, default=0, help="first sample index (new file only)")
    parser.add_argument("--limit", type=int, help="stop after N samples (testing)")
    args = parser.parse_args()
    base = args.host if "://" in args.host else f"http://{args.host}"
    try:
        download(base.rstrip("/"), args.out, args.block, args.pause,
                 args.max_failures, args.start, args.limit)
    except KeyboardInterrupt:
        print("\ninterrupted; run again to resume", file=sys.stderr)
        return 130
    except DeviceError as err:
        print(f"giving up: {err}\nrun again later to resume", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
