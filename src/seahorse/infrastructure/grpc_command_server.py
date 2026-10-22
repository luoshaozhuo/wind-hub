"""仅回环监听的虚拟 Commander RPC。

CONFIRMED_SUCCESS 表示模型已接受控制状态更改，不表示机组物理动作已完成。
超时或断线返回 UNKNOWN；不得对同一个 command_id 自动重试。
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

import grpc

from core.infrastructure.rpc import commander_pb2 as pb
from seahorse.application.command_queue import CommandQueue
from seahorse.domain.commands import EquipmentCommand
from seahorse.domain.models import Scalar
from seahorse.infrastructure.models.wind_turbine.contract import COMMAND_POINTS


def _decode(value: pb.ScalarValue) -> Scalar:
    kind = value.WhichOneof("kind")
    if kind == "bool_value":
        return bool(value.bool_value)
    if kind == "sint64_value":
        return int(value.sint64_value)
    if kind == "uint64_value":
        return int(value.uint64_value)
    if kind == "double_value":
        number = float(value.double_value)
        if not math.isfinite(number):
            raise ValueError("non-finite value")
        return number
    raise ValueError("unsupported command scalar")


class SeahorseCommanderService:
    """本入口只开放单点控制 RPC，不声明自己是完整 Commander。"""

    def __init__(self, queue: CommandQueue, turbine_ids: frozenset[str]) -> None:
        self._queue = queue
        self._turbine_ids = turbine_ids

    async def write_point(
        self, request: pb.WritePointRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.CommandResultMessage:
        if (
            not request.command_id
            or request.device_id not in self._turbine_ids
            or request.point_id not in COMMAND_POINTS
        ):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unmapped command")
        try:
            value = _decode(request.value)
            outcome = self._queue.enqueue(
                EquipmentCommand(request.command_id, request.device_id, request.point_id, value)
            )
        except (TypeError, ValueError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except RuntimeError as exc:
            status = (
                grpc.StatusCode.RESOURCE_EXHAUSTED
                if "queue full" in str(exc)
                else grpc.StatusCode.FAILED_PRECONDITION
            )
            await context.abort(status, str(exc))
        timeout = min(request.timeout if request.timeout > 0 else 3.0, 30.0)
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(outcome)), timeout)
        except TimeoutError:
            return pb.CommandResultMessage(
                command_id=request.command_id,
                outcome=pb.COMMAND_OUTCOME_UNKNOWN,
                error="execution timed out; do not retry without reconciliation",
            )
        except (ValueError, TypeError, KeyError) as exc:
            return pb.CommandResultMessage(
                command_id=request.command_id,
                outcome=pb.COMMAND_OUTCOME_CONFIRMED_FAILURE,
                error=str(exc),
            )
        except RuntimeError as exc:
            return pb.CommandResultMessage(
                command_id=request.command_id,
                outcome=pb.COMMAND_OUTCOME_UNKNOWN,
                error=str(exc),
            )
        return pb.CommandResultMessage(
            command_id=request.command_id,
            outcome=pb.COMMAND_OUTCOME_CONFIRMED_SUCCESS,
        )


async def start_command_server(
    queue: CommandQueue, turbine_ids: frozenset[str], bind: str
) -> grpc.aio.Server:
    """仅 loopback；外网部署必须先增加身份认证。"""
    if not (bind.startswith("127.0.0.1:") or bind.startswith("[::1]:")):
        raise ValueError("command RPC must bind loopback")
    server = grpc.aio.server()
    handler = grpc.unary_unary_rpc_method_handler(
        SeahorseCommanderService(queue, turbine_ids).write_point,
        request_deserializer=pb.WritePointRequest.FromString,
        response_serializer=pb.CommandResultMessage.SerializeToString,
    )
    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler(
            "windhub.commander.v1.CommanderService", {"WritePoint": handler}
        ),
    ))
    if server.add_insecure_port(bind) == 0:
        raise RuntimeError("cannot bind command endpoint")
    await server.start()
    return server
