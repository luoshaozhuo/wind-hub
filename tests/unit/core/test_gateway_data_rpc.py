"""Gateway gRPC Struct 反馈契约。"""
from __future__ import annotations

import pytest
from google.protobuf import struct_pb2

from core.infrastructure.rpc.gateway_data import decode_feedback, encode_feedback


def test_feedback_round_trip_preserves_bool_int_float() -> None:
    values = {
        ("wt1", "enabled"): True,
        ("wt1", "status"): 3,
        ("wt1", "power"): 123.5,
    }
    assert decode_feedback(encode_feedback(values)) == values


def test_duplicate_rejected() -> None:
    request = struct_pb2.Struct()
    request.update({
        "points": [
            {"device_id": "wt1", "point_id": "p", "value": {"int_value": "1"}},
            {"device_id": "wt1", "point_id": "p", "value": {"int_value": "2"}},
        ]
    })
    with pytest.raises(ValueError, match="duplicate"):
        decode_feedback(request)


def test_text_scalar_and_unsafe_integer_rejected() -> None:
    with pytest.raises(ValueError):
        encode_feedback({("wt1", "text"): "running"})
    with pytest.raises(ValueError):
        encode_feedback({("wt1", "large"): 1 << 54})
