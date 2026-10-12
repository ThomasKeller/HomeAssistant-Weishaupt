"""History logic tests (no Home Assistant needed)."""
import asyncio
import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone, tzinfo

_base = pathlib.Path(__file__).parent.parent / "custom_components" / "weishaupt"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _base / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


history = _load("history")
protocol = _load("protocol")

UTC = timezone.utc


def _last_sunday(year, month):
    day = datetime(year, month, 31 if month in (3, 10) else 30)
    return day - timedelta(days=(day.weekday() + 1) % 7)


class _Berlin(tzinfo):
    """Europe/Berlin (EU rules) without needing a tz database."""

    def utcoffset(self, dt):
        naive = dt.replace(tzinfo=None)
        start = _last_sunday(naive.year, 3).replace(hour=2)  # 02:00 -> 03:00
        end = _last_sunday(naive.year, 10)
        ambiguous_from, ambiguous_to = end.replace(hour=2), end.replace(hour=3)
        if naive < start:
            return timedelta(hours=1)
        if naive < ambiguous_from:
            return timedelta(hours=2)
        if naive < ambiguous_to:
            return timedelta(hours=1) if dt.fold else timedelta(hours=2)
        return timedelta(hours=1)

    def dst(self, dt):
        return self.utcoffset(dt) - timedelta(hours=1)

    def tzname(self, dt):
        return "CEST" if self.dst(dt) else "CET"


def _berlin():
    return _Berlin()


def test_parse_real_rows_and_skip_garbage():
    text = (
        "12.10.26 22:41:08;18.3;22.4;47.0;9.0;35.0;8.0;9.7;0\r"
        "12.10.26 22:42:08;18.3;22.4;47.0;9.0;35.0;8.0;9.7;0\r"
        "12.10.26 22:43:08;18.3;22.4;47\x00\r"  # truncated
        "\x00\x00"
    )
    rows = history.parse_csv(text)
    assert len(rows) == 2
    assert rows[0][0] == datetime(2026, 10, 12, 22, 41, 8)
    assert rows[0][1] == (18.3, 22.4, 47.0, 9.0, 35.0, 8.0, 9.7, 0.0)


def test_invalid_marker_cell_is_none():
    rows = history.parse_csv("12.10.26 22:41:08;-3276.8;22.4;47.0;9.0;35.0;8.0;9.7;0")
    assert rows[0][1][0] is None and rows[0][1][1] == 22.4


def test_decimal_comma_is_accepted():
    rows = history.parse_csv("12.10.26 22:41:08;18,3;22.4;47.0;9.0;35.0;8.0;9.7;0")
    assert rows[0][1][0] == 18.3


def test_localizer_summer_and_winter_offsets():
    tz = _berlin()
    loc = history.Localizer(tz)
    assert loc.to_utc(datetime(2026, 10, 12, 22, 41, 8)) == datetime(2026, 10, 12, 20, 41, 8, tzinfo=UTC)
    loc = history.Localizer(tz)
    assert loc.to_utc(datetime(2026, 12, 1, 12, 0, 0)) == datetime(2026, 12, 1, 11, 0, 0, tzinfo=UTC)


def test_localizer_resolves_repeated_dst_hour():
    tz = _berlin()
    loc = history.Localizer(tz)
    # 25.10.2026: 03:00 CEST -> 02:00 CET, so 02:xx happens twice
    first = loc.to_utc(datetime(2026, 10, 25, 2, 59, 8))
    second = loc.to_utc(datetime(2026, 10, 25, 2, 0, 8))  # clock went back
    third = loc.to_utc(datetime(2026, 10, 25, 3, 0, 8))
    assert first == datetime(2026, 10, 25, 0, 59, 8, tzinfo=UTC)
    assert second == datetime(2026, 10, 25, 1, 0, 8, tzinfo=UTC)
    assert third == datetime(2026, 10, 25, 2, 0, 8, tzinfo=UTC)
    assert first < second < third


def test_localizer_rejects_time_going_back():
    loc = history.Localizer(UTC)
    assert loc.to_utc(datetime(2026, 1, 1, 12, 0)) is not None
    assert loc.to_utc(datetime(2026, 1, 1, 11, 0)) is None


def test_hourly_aggregation():
    agg = history.HourlyAggregator(columns=2)
    t = datetime(2026, 1, 1, 10, 0, 8, tzinfo=UTC)
    out = []
    for i in range(60):  # a full hour, minute samples
        out += agg.add(t + timedelta(minutes=i), (float(i), 5.0))
    assert out == []  # hour not complete yet
    out = agg.add(t + timedelta(minutes=60), (100.0, None))
    by_col = {col: stat for col, stat in out}
    assert by_col[0].start == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert by_col[0].mean == 29.5 and by_col[0].minimum == 0.0 and by_col[0].maximum == 59.0
    assert by_col[1].mean == 5.0
    rest = {col: s for col, s in agg.flush()}
    assert rest[0].mean == 100.0 and 1 not in rest  # column 1 only had None


def test_find_first_index_at_or_after():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    calls = []

    async def get_time(i):
        calls.append(i)
        return base + timedelta(minutes=i)

    target = base + timedelta(minutes=12345)
    idx = asyncio.run(history.find_first_index_at_or_after(100000, target, get_time))
    assert idx == 12345
    assert len(calls) <= 18
    assert asyncio.run(history.find_first_index_at_or_after(10, base + timedelta(days=1), get_time)) == 10
    assert asyncio.run(history.find_first_index_at_or_after(10, base - timedelta(days=1), get_time)) == 0


def test_parse_counter_little_endian_u32():
    # real device sample: 23 + 60*256 + 23*65536 = 1522711
    text = '{"prot":"coco","telegramm":[[0,0,1,20004,0,0,23,60,23,0]]}'
    assert protocol.parse_counter(text, (0, 20004)) == 1522711
