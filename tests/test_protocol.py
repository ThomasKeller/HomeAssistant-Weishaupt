"""Protocol tests; run with: python -m pytest tests (no Home Assistant needed)."""
import importlib.util
import json
import pathlib

_path = (
    pathlib.Path(__file__).parent.parent
    / "custom_components"
    / "weishaupt"
    / "protocol.py"
)
_spec = importlib.util.spec_from_file_location("protocol", _path)
protocol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(protocol)


def row(index, low, high, module=10):
    return [module, 0, 1, index, 0, 0, low, high]


def body(*rows):
    return json.dumps({"prot": "coco", "telegramm": list(rows)})


def raises(exc, func, *args):
    try:
        func(*args)
    except exc:
        return True
    return False


def test_positive_with_high_byte():
    # real device sample: hot water 204 + 1*256 = 460 -> 46.0 degC
    assert protocol.decode_value(row(14, 204, 1)) == ((10, 14), 460)


def test_negative_value_keeps_sign():
    # -2.5 degC -> raw -25 -> 0xFFE7
    assert protocol.decode_value(row(12, 0xE7, 0xFF)) == ((10, 12), -25)


def test_invalid_marker_is_none():
    # real device sample: buffer sensor not connected (0x8000)
    assert protocol.decode_value(row(118, 0, 128)) == ((10, 118), None)


def test_build_request_with_module():
    assert protocol.build_request([(10, 1), (6, 17)])["telegramm"] == [
        [10, 0, 1, 1, 0, 0, 0, 0],
        [6, 0, 1, 17, 0, 0, 0, 0],
    ]


def test_same_info_number_different_module():
    keys = [(10, 2), (6, 2)]
    text = body(row(2, 80, 0, 10), row(2, 183, 0, 6))
    assert protocol.parse_response(text, keys) == {(10, 2): 80, (6, 2): 183}


def test_unrequested_rows_are_ignored():
    text = body(row(12, 90, 0), row(328, 1, 0, 0))
    assert protocol.parse_response(text, [(10, 12)]) == {(10, 12): 90}


def test_unsupported_row_is_absent():
    # device omits info numbers it does not know (e.g. 374)
    text = body(row(12, 90, 0))
    assert protocol.parse_response(text, [(10, 12), (10, 374)]) == {(10, 12): 90}


def test_busy_page_raises_busy():
    page = "<HTML><BODY>Sorry, the server is busy.</BODY></HTML>"
    assert raises(protocol.DeviceBusyError, protocol.parse_response, page, [(10, 12)])


def test_nul_bytes_raise():
    assert raises(protocol.ProtocolError, protocol.parse_response, "\x00" * 21, [(10, 12)])


def test_interleaved_foreign_telegram_raises():
    # real device sample
    text = (
        '{"prot":"coco","telegramm":[{"prot":"coco","telegramm":'
        "[[0,0,1,328,0,0,1,0]\x00\x00"
    )
    assert raises(protocol.ProtocolError, protocol.parse_response, text, [(10, 138)])


def test_only_foreign_rows_raise():
    text = body(row(328, 1, 0, 0))
    assert raises(protocol.ProtocolError, protocol.parse_response, text, [(10, 12)])


def test_junk_rows_are_skipped():
    text = body(row(12, 90, 0), "garbage", [1, 2, 3])
    assert protocol.parse_response(text, [(10, 12)]) == {(10, 12): 90}


def test_scale_keeps_whole_numbers_for_factor_one():
    value = protocol.scale(0, 1.0)  # const.py uses 1.0 as default factor
    assert value == 0 and isinstance(value, int)
    assert str(protocol.scale(373, 1.0)) == "373"


def test_scale_applies_factors():
    assert protocol.scale(90, 0.1) == 9.0
    assert protocol.scale(97, 0.1) == 9.7
    assert protocol.scale(207, 1000) == 207000
    assert protocol.scale(-25, 0.1) == -2.5
