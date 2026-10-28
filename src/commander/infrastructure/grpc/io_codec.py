"""Commander 强类型 I/O Protobuf 与领域模型转换。"""

from __future__ import annotations

from core.application.command import Command, CommandResult
from core.application.protocol_contract import WriteOutcome
from contracts import commander_pb2 as pb
from core.infrastructure.rpc.scalar_codec import decode_scalar, encode_scalar

from ...application.session import PointReading

__all__ = [
    "command_from_proto",
    "command_result_to_proto",
    "command_to_proto",
    "decode_scalar",
    "encode_scalar",
    "point_value_to_proto",
]


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
    if message.value.WhichOneof("kind") is None:
        raise ValueError("command value must not be empty")
    return Command(
        command_id=message.command_id,
        device_id=message.device_id,
        point_id=message.point_id,
        value=decode_scalar(message.value),
        timeout=message.timeout,
    )


_OUTCOME_TO_PROTO: dict[WriteOutcome, pb.CommandOutcome.ValueType] = {
    WriteOutcome.CONFIRMED_SUCCESS: pb.COMMAND_OUTCOME_CONFIRMED_SUCCESS,
    WriteOutcome.CONFIRMED_FAILURE: pb.COMMAND_OUTCOME_CONFIRMED_FAILURE,
    WriteOutcome.UNKNOWN: pb.COMMAND_OUTCOME_UNKNOWN,
}


def command_result_to_proto(result: CommandResult) -> pb.CommandResultMessage:
    """领域 CommandResult → wire message。"""
    message = pb.CommandResultMessage(
        command_id=result.command_id,
        outcome=_OUTCOME_TO_PROTO[result.outcome],
        error=result.error or "",
    )
    message.finished_at.FromDatetime(result.finished_at)
    return message
