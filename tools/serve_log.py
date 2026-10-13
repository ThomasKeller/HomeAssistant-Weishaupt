#!/usr/bin/env python3
"""Serve a log file made by download_log.py like a Weishaupt WCM-COM would.

Home Assistant's ``weishaupt.import_history`` can read from this server
(parameter ``source``) instead of the slow device::

    python serve_log.py --file weishaupt_log.csv --port 8765

then call the service with ``source: <ip of this computer>:8765``.

Only the two requests used by the import are answered:
``GET /x.csv?a=<first>&b=<end>`` and the sample counter via
``POST /parameter.json``. The file is re-read when it changes, so a download
that catches up with new samples is picked up without a restart.

Only the Python standard library is needed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

COUNTER_INFO = 20004


class LogFile:
    """The log lines of a download_log.py output file (reloaded on change)."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._stamp: tuple[float, int] | None = None
        self.first = 0
        self.lines: list[str] = []

    def refresh(self) -> None:
        stat = os.stat(self._path)
        stamp = (stat.st_mtime, stat.st_size)
        with self._lock:
            if stamp == self._stamp:
                return
            meta = self._path + ".meta.json"
            first = 0
            if os.path.exists(meta):
                with open(meta, encoding="utf-8") as handle:
                    first = json.load(handle)["first_index"]
            with open(self._path, encoding="utf-8") as handle:
                lines = [ln.strip() for ln in handle if ln.strip()]
            self.first, self.lines, self._stamp = first, lines, stamp

    @property
    def count(self) -> int:
        return self.first + len(self.lines)

    def block(self, first: int, end: int) -> list[str]:
        start = max(first - self.first, 0)
        stop = max(min(end - self.first, len(self.lines)), 0)
        return self.lines[start:stop]


def make_handler(log: LogFile):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str, content_type: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path != "/x.csv":
                self._send("not found", "text/plain", 404)
                return
            query = parse_qs(url.query)
            try:
                first, end = int(query["a"][0]), int(query["b"][0])
            except (KeyError, ValueError, IndexError):
                self._send("bad request", "text/plain", 400)
                return
            log.refresh()
            # like the device: CR separated, no header
            self._send("\r".join(log.block(first, end)) + "\r", "text/csv")

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/parameter.json":
                self._send("not found", "text/plain", 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                rows = json.loads(self.rfile.read(length))["telegramm"]
            except (ValueError, KeyError, TypeError):
                self._send("bad request", "text/plain", 400)
                return
            log.refresh()
            count = log.count
            answer = []
            for row in rows:
                if isinstance(row, list) and len(row) >= 4 and row[3] == COUNTER_INFO:
                    answer.append(
                        [row[0], 0, 1, COUNTER_INFO, 0, 0]
                        + [(count >> shift) & 0xFF for shift in (0, 8, 16, 24)]
                    )
            self._send(
                json.dumps({"prot": "coco", "telegramm": answer}, separators=(",", ":")),
                "application/json",
            )

        def log_message(self, fmt: str, *args) -> None:  # quiet
            pass

    return Handler


def serve(path: str, bind: str, port: int) -> ThreadingHTTPServer:
    log = LogFile(path)
    log.refresh()
    return ThreadingHTTPServer((bind, port), make_handler(log))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--file", default="weishaupt_log.csv")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = serve(args.file, args.bind, args.port)
    log = LogFile(args.file)
    log.refresh()
    print(
        f"serving {len(log.lines)} samples (index {log.first}..{log.count - 1}) "
        f"on http://{args.bind}:{args.port}  - Ctrl+C to stop"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
