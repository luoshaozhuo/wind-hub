"""Seahorse 虚拟设备命令 RPC 入口，仅绑定受信任的本机地址。

复用 Commander WritePoint wire contract，不复制真实 Commander 的控制逻辑。
CONFIRMED_SUCCESS 只意味着入队接收，不代表物理执行完成。
"""
from __future__ import annotations

import math
from typing import Any

import grpc

from core.infrastructure.rpc import commander_pb2 as pb
from core.infrastructure.rpc import commander_pb2_grpc as pb_grpc
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


class SeahorseCommanderService(pb_grpc.CommanderServiceServicer):
    """仅实现虚拟设备的 WritePoint；其他 RPC 保持未实现。"""

    def __init__(self, queue: CommandQueue, turbine_ids: frozenset[str]) -> None:
        self._queue = queue
        self._turbine_ids = turbine_ids

    async def WritePoint(
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
            self._queue.enqueue(
                EquipmentCommand(
                    request.command_id, request.device_id, request.point_id, value
                )
            )
        except (TypeError, ValueError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except RuntimeError as exc:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(exc))
        # 现有协议没有 ACCEPTED 状态；不能冒充实际执行成功。
        # UNKNOWN 强制 Gateway 不盲目重试，最终执行靠回读确认。
        return pb.CommandResultMessage(
            command_id=request.command_id,
            outcome=pb.COMMAND_OUTCOME_UNKNOWN,
            error="accepted into simulation queue; execution not yet confirmed",
        )


async def start_command_server(
    queue: CommandQueue, turbine_ids: frozenset[str], bind: str
) -> grpc.aio.Server:
    """仅监听 loopback；跨主机部署需要单独增加身份认证，不默认开放。"""
    if not (bind.startswith("127.0.0.1:") or bind.startswith("[::1]:")):
        raise ValueError("command RPC must bind loopback")
    server = grpc.aio.server()
    pb_grpc.add_CommanderServiceServicer_to_server(
        SeahorseCommanderService(queue, turbine_ids), server
    )
    if server.add_insecure_port(bind) == 0:
        raise RuntimeError("cannot bind command endpoint")
    await server.start()
    return server
