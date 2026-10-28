"""共享 ScalarValue 标量编解码契约（common.proto 强类型 oneof）。"""

from __future__ import annotations

import math

import pytest

from contracts import common_pb2 as pb
from core.infrastructure.rpc.scalar_codec import decode_scalar, encode_scalar


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


def test_nonfinite_floats_remain_decodable_at_codec_layer() -> None:
    # 非有限浮点是否允许进入业务由各 gRPC 服务边界（如 Gateway WriteMany）决定。
    for value in (math.nan, math.inf, -math.inf):
        message = encode_scalar(value)
        assert message.WhichOneof("kind") == "double_value"
        decoded = decode_scalar(message)
        if math.isnan(value):
            assert math.isnan(decoded)
        else:
            assert decoded == value


def test_unsupported_type_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported scalar value type"):
        encode_scalar([1, 2])  # type: ignore[arg-type]


def test_unset_scalar_decodes_to_none() -> None:
    assert decode_scalar(pb.ScalarValue()) is None
