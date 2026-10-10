"""Commander 强类型 I/O Protobuf 与领域模型转换。"""

from __future__ import annotations

from typing import Any

from ...application.command import Command, CommandResult
from ...application.session import PointReading
from . import commander_pb2 as pb


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


def point_value_to_proto(value: PointReading) -> pb.PointValueMessage:
    """领域 PointReading → wire message。"""
    message = pb.PointValueMessage(
        device_id=value.device_id,
        point_id=value.point_id,
        value=encode_scalar(value.value),
        quality=value.quality.value,
        source=value.source or "",
        timestamp_source=value.timestamp_source,
    )
    message.timestamp.FromDatetime(value.timestamp)
    return message


def command_to_proto(command: Command) -> pb.WritePointRequest:
    """领域 Command → wire request。"""
    return pb.WritePointRequest(
        command_id=command.command_id,
        device_id=command.device_id,
        point_id=command.point_id,
        value=encode_scalar(command.value),
        timeout=command.timeout,
    )


def command_from_proto(message: pb.WritePointRequest) -> Command:
    """wire request → 领域 Command。"""
    return Command(
        command_id=message.command_id,
        device_id=message.device_id,
        point_id=message.point_id,
        value=decode_scalar(message.value),
        timeout=message.timeout,
    )


def command_result_to_proto(result: CommandResult) -> pb.CommandResultMessage:
    """领域 CommandResult → wire message。"""
    message = pb.CommandResultMessage(
        command_id=result.command_id,
        success=result.success,
        error=result.error or "",
    )
    message.finished_at.FromDatetime(result.finished_at)
    return message
