"""Gateway 出站 Commander RPC：隔离线协议编码和传输异常。"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import grpc

from core.infrastructure.rpc import commander_pb2 as pb
from core.infrastructure.rpc import commander_pb2_grpc
from industrial_gateway.application.command_service import (
    CommanderUnavailableError,
    CommandOutcome,
    CommandRequest,
    CommandResult,
)
from industrial_gateway.domain import Scalar

if TYPE_CHECKING:
    from core.infrastructure.rpc.commander_pb2_grpc import CommanderServiceAsyncStub


class GrpcCommanderWriter:
    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._stub = cast(
            "CommanderServiceAsyncStub", commander_pb2_grpc.CommanderServiceStub(channel)
        )

    async def write_point(self, request: CommandRequest, *, timeout: float) -> CommandResult:
        command = pb.WritePointRequest(
            command_id=request.command_id,
            device_id=request.device_id,
            point_id=request.point_id,
            value=_encode(request.value),
            timeout=request.timeout,
        )
        try:
            result = await self._stub.WritePoint(command, timeout=timeout)
        except grpc.RpcError as exc:
            raise CommanderUnavailableError(str(exc)) from exc
        outcomes = {
            pb.COMMAND_OUTCOME_CONFIRMED_SUCCESS: CommandOutcome.CONFIRMED_SUCCESS,
            pb.COMMAND_OUTCOME_CONFIRMED_FAILURE: CommandOutcome.CONFIRMED_FAILURE,
        }
        return CommandResult(outcomes.get(result.outcome, CommandOutcome.UNKNOWN), result.error)


def _encode(value: Scalar) -> pb.ScalarValue:
    if isinstance(value, bool):
        return pb.ScalarValue(bool_value=value)
    if isinstance(value, int):
        if value < 0:
            return pb.ScalarValue(sint64_value=value)
        return pb.ScalarValue(uint64_value=value)
    return pb.ScalarValue(double_value=value)
