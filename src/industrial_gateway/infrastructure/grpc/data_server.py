"""Gateway 批量反馈 gRPC 服务；只写虚拟机组反馈点，不开放控制。"""

from __future__ import annotations

import grpc

from contracts import gateway_pb2 as pb
from contracts import gateway_pb2_grpc as rpc
from core.infrastructure.rpc.scalar_codec import decode_scalar
from industrial_gateway.application.point_store import PointStore


class GatewayDataService(rpc.GatewayDataServiceServicer):
    def __init__(self, store: PointStore) -> None:
        self._store = store

    async def WriteMany(
        self, request: pb.WriteManyRequest, context: grpc.aio.ServicerContext
    ) -> pb.WriteManyResponse:
        try:
            if not 0 < len(request.points) <= 5000:
                raise ValueError("feedback batch must contain 1..5000 points")
            values: dict[tuple[str, str], bool | int | float] = {}
            for point in request.points:
                if not point.device_id or not point.point_id:
                    raise ValueError("feedback point requires device_id and point_id")
                if not point.HasField("value"):
                    raise ValueError("feedback point requires value")
                value = decode_scalar(point.value)
                if type(value) not in (bool, int, float):
                    raise ValueError("feedback must be numeric or bool")
                key = (point.device_id, point.point_id)
                if key in values:
                    raise ValueError("duplicate feedback point")
                values[key] = value
            await self._store.update_batch(values, source="grpc")
        except (KeyError, TypeError, ValueError, PermissionError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("gRPC abort unexpectedly returned") from exc
        return pb.WriteManyResponse(updated=len(values))


def create_grpc_server(store: PointStore, bind: str) -> grpc.aio.Server:
    """无身份认证时只允许本机监听。"""
    if not (bind.startswith("127.0.0.1:") or bind.startswith("[::1]:")):
        raise ValueError("Gateway data gRPC must bind loopback")
    server = grpc.aio.server()
    rpc.add_GatewayDataServiceServicer_to_server(GatewayDataService(store), server)
    if server.add_insecure_port(bind) == 0:
        raise RuntimeError("unable to bind Gateway data gRPC")
    return server
