"""Tests for tools/download_log.py and tools/serve_log.py (no Home Assistant needed)."""
import importlib.util
import os
import pathlib
import sys
import tempfile
import threading
from datetime import datetime, timedelta

_root = pathlib.Path(__file__).parent.parent


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


download_log = _load(_root / "tools" / "download_log.py", "download_log")
serve_log = _load(_root / "tools" / "serve_log.py", "serve_log")
history = _load(_root / "custom_components" / "weishaupt" / "history.py", "history_t")
protocol = _load(_root / "custom_components" / "weishaupt" / "protocol.py", "protocol_t")


def _sample_lines(count, first_minute=0):
    base = datetime(2026, 1, 1, 0, 0, 8)
    return [
        f"{(base + timedelta(minutes=first_minute + i)):%d.%m.%y %H:%M:%S}"
        f";18.3;22.4;47.0;{(i % 10) / 10:.1f};35.0;8.0;9.7;0"
        for i in range(count)
    ]


class _Server:
    def __init__(self, directory, lines, first=1000):
        self.path = os.path.join(directory, "source.csv")
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        with open(self.path + ".meta.json", "w", encoding="utf-8") as handle:
            handle.write('{"first_index": %d}' % first)
        self.httpd = serve_log.serve(self.path, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_counter_and_blocks_are_readable_by_the_integration_parsers():
    with tempfile.TemporaryDirectory() as tmp:
        lines = _sample_lines(2500)
        server = _Server(tmp, lines, first=1000)
        try:
            counter = download_log.read_counter(server.base)
            assert counter == 3500
            # the same parsers the Home Assistant service uses
            text = download_log._get(
                server.base + "/parameter.json",
                b'{"prot":"coco","telegramm":[[0,0,1,20004,0,0,0,0]]}',
                10,
            )
            assert protocol.parse_counter(text, (0, 20004)) == 3500
            rows = history.parse_csv(
                download_log._get(server.base + "/x.csv?a=1000&b=1003", None, 10)
            )
            assert len(rows) == 3
            assert rows[0][0] == datetime(2026, 1, 1, 0, 0, 8)
        finally:
            server.close()


def test_download_is_complete_and_resumable():
    with tempfile.TemporaryDirectory() as tmp:
        lines = _sample_lines(2500)
        server = _Server(tmp, lines, first=1000)
        out = os.path.join(tmp, "dl.csv")
        quiet = lambda *_: None  # noqa: E731
        try:
            # interrupted after 1200 samples (limit), then resumed
            nxt = download_log.download(
                server.base, out, block=500, pause=0, start=1000, limit=1200, log=quiet
            )
            assert nxt == 2200
            nxt = download_log.download(server.base, out, block=500, pause=0, log=quiet)
            assert nxt == 3500
            with open(out, encoding="utf-8") as handle:
                assert handle.read().split("\n")[:-1] == lines
        finally:
            server.close()


def test_download_catches_up_with_new_samples():
    with tempfile.TemporaryDirectory() as tmp:
        lines = _sample_lines(1500)
        server = _Server(tmp, lines[:1000], first=0)
        out = os.path.join(tmp, "dl.csv")
        quiet = lambda *_: None  # noqa: E731
        try:
            download_log.download(server.base, out, block=400, pause=0, log=quiet)
            # device logs 500 more samples; server picks the changed file up
            with open(server.path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines) + "\n")
            nxt = download_log.download(server.base, out, block=400, pause=0, log=quiet)
            assert nxt == 1500
            with open(out, encoding="utf-8") as handle:
                assert handle.read().split("\n")[:-1] == lines
        finally:
            server.close()


def test_empty_channel_value_becomes_none_in_import_parser():
    rows = history.parse_csv("21.11.23 15:53:31;44.0;20.0;48.0;9.0;51.0;;8.4;0")
    assert len(rows) == 1
    assert rows[0][1] == (44.0, 20.0, 48.0, 9.0, 51.0, None, 8.4, 0.0)


def test_garbage_block_is_rejected():
    assert download_log.LINE.match("12.10.26 22:41:08;18.3;22.4;47.0;9.0;35.0;8.0;9.7;0")
    # real log line: a channel without a reading is an empty field
    assert download_log.LINE.match("21.11.23 15:53:31;44.0;20.0;48.0;9.0;51.0;;8.4;0")
    assert not download_log.LINE.match("12.10.26 22:43:08;18.3;22.4;47")
    assert not download_log.LINE.match("<HTML>Sorry, the server is busy.</HTML>")
