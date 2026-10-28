"""共享 RPC 标量编解码。

Collector、Commander、Gateway 共用 ``windhub.common.v1.ScalarValue``
作为设备点值的 wire 表示；编解码逻辑因此归属 Core，供 Collector 与
Commander 的 gRPC 适配器复用，避免进程间相互 import。
"""

from __future__ import annotations

from typing import Any

from contracts import common_pb2 as pb


def encode_scalar(value: Any) -> pb.ScalarValue:
    """把设备标量编码为不会丢失 64 位整数精度的 Protobuf oneof。"""
    message = pb.ScalarValue()
    if value is None:
        return message
    if isinstance(value, bool):
        message.bool_value = value
    elif isinstance(value, int):
        if -(2**63) <= value <= 2**63 - 1:
            message.sint64_value = value
        elif 0 <= value <= 2**64 - 1:
            message.uint64_value = value
        else:
            raise ValueError("integer value is outside protobuf 64-bit range")
    elif isinstance(value, float):
        message.double_value = value
    elif isinstance(value, str):
        message.string_value = value
    elif isinstance(value, bytes):
        message.bytes_value = value
    else:
        raise ValueError(f"unsupported scalar value type: {type(value).__name__}")
    return message


def decode_scalar(message: pb.ScalarValue) -> Any:
    """把 ScalarValue 恢复为 Python 标量。"""
    kind = message.WhichOneof("kind")
    if kind is None:
        return None
    return getattr(message, kind)
