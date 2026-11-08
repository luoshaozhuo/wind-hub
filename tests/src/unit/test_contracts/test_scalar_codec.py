"""共享 ScalarValue 标量编解码契约（common.proto 强类型 oneof）。"""

from __future__ import annotations

import math

import pytest

from contracts import common_pb2 as pb
from contracts.scalar_codec import decode_scalar, encode_scalar


def test_round_trip_preserves_bool_int_float_str_bytes_none() -> None:
    values: list[bool | int | float | str | bytes | None] = [
        True,
        False,
        3,
        -(2**63),
        2**63,
        2**64 - 1,
        123.5,
        "running",
        b"raw",
        None,
    ]
    for value in values:
        assert decode_scalar(encode_scalar(value)) == value


def test_64bit_integers_keep_full_precision() -> None:
    large = (1 << 54) + 17
    message = encode_scalar(large)
    assert message.WhichOneof("kind") == "sint64_value"
    assert decode_scalar(message) == large

    unsigned = 2**64 - 1
    message = encode_scalar(unsigned)
    assert message.WhichOneof("kind") == "uint64_value"
    assert decode_scalar(message) == unsigned


def test_integer_outside_64bit_range_rejected() -> None:
    with pytest.raises(ValueError, match="64-bit range"):
        encode_scalar(2**64)
    with pytest.raises(ValueError, match="64-bit range"):
        encode_scalar(-(2**63) - 1)


def test_nonfinite_floats_rejected_at_codec_layer() -> None:
    # Contracts v1_6：统一编解码器明确拒绝 NaN/Infinity（设计 §ScalarValue 表）。
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="finite"):
            encode_scalar(value)
        # 绕过编码器直接构造 wire 消息时，解码侧同样必须拒绝。
        message = pb.ScalarValue()
        message.double_value = value
        with pytest.raises(ValueError, match="finite"):
            decode_scalar(message)


def test_unsupported_type_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported scalar value type"):
        encode_scalar([1, 2])  # type: ignore[arg-type]


def test_unset_scalar_decodes_to_none() -> None:
    assert decode_scalar(pb.ScalarValue()) is None


pytestmark = [pytest.mark.unit, pytest.mark.contract]
