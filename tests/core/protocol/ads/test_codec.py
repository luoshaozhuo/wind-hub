"""ADS codec 纯单元测试（无 pyads、无连接）。

覆盖标量解码/写入强转、STRING 定宽、Sum Write 头编译、通知区块分组/
重叠/超界与 Sum Write 错误响应校验。
"""

from __future__ import annotations

import ctypes
import struct

import pytest

from core.application.errors import ConfigError, ProtocolError
from core.infrastructure.protocol.ads.codec import (
    NotificationBlock,
    _coerce_write_value,
    _decode_sum_write_errors,
    _encode_value,
    compile_decoder,
    compile_notification_blocks,
    encode_sum_write,
)
from core.infrastructure.protocol.ads.mapping import ADSPoint


def _point(
    point_id: str = "p",
    data_type: str = "INT",
    size: int = 2,
    *,
    group: int = 0x4020,
    offset: int = 0,
    symbol: str = "MAIN.p",
) -> ADSPoint:
    return ADSPoint(
        point_id=point_id,
        index_group=group,
        index_offset=offset,
        data_type=data_type,
        size=size,
        symbol=symbol,
    )


# --------------------------------------------------------------------------
# compile_decoder
# --------------------------------------------------------------------------


def test_decoder_reads_little_endian_scalar() -> None:
    point = _point(data_type="DINT", size=4)
    raw = struct.pack("<i", -123456)
    assert point.decoder(raw, 0) == -123456


def test_decoder_supports_memoryview_and_offset() -> None:
    point = _point(data_type="UINT", size=2)
    view = memoryview(b"\x00" * 3 + struct.pack("<H", 0x1234))
    assert point.decoder(view, 3) == 0x1234


def test_decoder_string_stops_at_nul_and_handles_short_buffer() -> None:
    point = _point(data_type="STRING", size=8)
    encoded = "风机".encode() + b"\x00" + b"tail"
    assert point.decoder(encoded, 0) == "风机"
    # 缓冲不足 8 字节时按实际可读长度解码，不抛异常。
    assert point.decoder(b"ab", 0) == "ab"


def test_decoder_size_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="size mismatch"):
        compile_decoder(_point(data_type="INT", size=4))


# --------------------------------------------------------------------------
# encode_sum_write / 通知区块
# --------------------------------------------------------------------------


def test_encode_sum_write_layout() -> None:
    point = _point(offset=10)
    payload = b"\x39\x05"
    chunk = ((0, point, payload),)
    raw = encode_sum_write(chunk)

    assert bytes(raw) == struct.pack("<III", point.group, 10, 2) + payload


def test_notification_blocks_group_by_group_and_span() -> None:
    points = [
        _point("a", group=1, offset=0),
        _point("b", group=1, offset=2),
        _point("c", group=2, offset=0),
    ]
    blocks = compile_notification_blocks(points, max_bytes=1024)

    assert len(blocks) == 2
    assert all(isinstance(block, NotificationBlock) for block in blocks)
    assert blocks[0].group == 1
    assert blocks[0].size == 4
    assert blocks[0].fields[0][1] == 0
    assert blocks[0].fields[1][1] == 2
    assert blocks[1].group == 2


def test_notification_blocks_split_when_span_exceeds_max_bytes() -> None:
    points = [_point("a", offset=0), _point("b", offset=100)]
    blocks = compile_notification_blocks(points, max_bytes=50)
    assert len(blocks) == 2


def test_notification_blocks_reject_nonpositive_max_bytes() -> None:
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        compile_notification_blocks([_point()], max_bytes=0)


def test_notification_blocks_reject_oversized_point() -> None:
    with pytest.raises(ValueError, match="exceeds notification block size"):
        compile_notification_blocks([_point(data_type="LREAL", size=8)], max_bytes=4)


def test_notification_blocks_reject_overlap() -> None:
    points = [_point("a", offset=0), _point("b", offset=1)]
    with pytest.raises(ConfigError, match="overlap"):
        compile_notification_blocks(points)


def test_notification_blocks_empty_input() -> None:
    assert compile_notification_blocks([]) == ()


# --------------------------------------------------------------------------
# Sum Write 错误响应
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "string", 1234, object()])
def test_decode_sum_write_errors_rejects_non_sequence(bad) -> None:
    with pytest.raises(ProtocolError, match="invalid response size"):
        _decode_sum_write_errors(bad, count=1)


def test_decode_sum_write_errors_rejects_wrong_length() -> None:
    with pytest.raises(ProtocolError, match="invalid response size"):
        _decode_sum_write_errors(b"\x00\x00\x00\x00", count=2)


def test_decode_sum_write_errors_accepts_ctypes_array_like_pyads() -> None:
    raw = struct.pack("<II", 0, 0x708)
    array_type = ctypes.c_ubyte * len(raw)
    errors = _decode_sum_write_errors(array_type(*raw), count=2)
    assert errors == (0, 0x708)


# --------------------------------------------------------------------------
# _encode_value
# --------------------------------------------------------------------------


def test_encode_scalar_matches_declared_width() -> None:
    assert _encode_value(True, _point(data_type="BOOL", size=1)) == b"\x01"
    assert _encode_value(-1, _point(data_type="INT", size=2)) == b"\xff\xff"


def test_encode_string_pads_and_rejects_overflow() -> None:
    point = _point(data_type="STRING", size=6)
    assert _encode_value("abc", point) == b"abc\x00\x00\x00"
    with pytest.raises(ValueError, match="exceeds configured size"):
        _encode_value("abcdef", point)  # 需要 NUL 终止，6 字符装不下
    with pytest.raises(TypeError, match="requires str"):
        _encode_value(1, point)


def test_encode_width_mismatch_rejected() -> None:
    # ADSPoint 构造期的 decoder 已拦截宽度不一致；此分支是编码器防御深度，
    # 用鸭子类型最小对象直接验证。
    from types import SimpleNamespace

    bad_point = SimpleNamespace(data_type="INT", size=4, point_id="p")
    with pytest.raises(ValueError, match="data width mismatch"):
        _encode_value(1, bad_point)


# --------------------------------------------------------------------------
# _coerce_write_value
# --------------------------------------------------------------------------


def test_coerce_bool_requires_exact_bool_type() -> None:
    assert _coerce_write_value(True, "BOOL") is True
    with pytest.raises(TypeError, match="requires bool"):
        _coerce_write_value(1, "BOOL")


@pytest.mark.parametrize(
    ("ads_type", "value", "expected"),
    [
        ("INT", -7, -7),
        ("UINT", 7.0, 7),
        ("DINT", 100.0, 100),
    ],
)
def test_coerce_integers(ads_type: str, value, expected: int) -> None:
    assert _coerce_write_value(value, ads_type) == expected


def test_coerce_rejects_fractional_integer_and_out_of_range() -> None:
    with pytest.raises(ValueError, match="requires an integer value"):
        _coerce_write_value(1.5, "INT")
    with pytest.raises(ValueError, match="outside range"):
        _coerce_write_value(70000, "UINT")
    with pytest.raises(TypeError, match="integer-compatible"):
        _coerce_write_value("1", "INT")
    with pytest.raises(TypeError, match="integer-compatible"):
        _coerce_write_value(True, "INT")


def test_coerce_float_types() -> None:
    assert _coerce_write_value(1, "REAL") == pytest.approx(1.0)
    with pytest.raises(TypeError, match="numeric value"):
        _coerce_write_value("1.0", "REAL")
    with pytest.raises(TypeError, match="numeric value"):
        _coerce_write_value(True, "LREAL")


def test_coerce_string_and_unknown_type() -> None:
    assert _coerce_write_value("reset", "STRING") == "reset"
    with pytest.raises(TypeError, match="string value"):
        _coerce_write_value(1, "STRING")
    with pytest.raises(ValueError, match="unsupported ADS type"):
        _coerce_write_value(1, "BOGUS")
