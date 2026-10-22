"""Gateway 批量反馈 gRPC 服务；不允许写入控制点。"""
from __future__ import annotations

import grpc
from google.protobuf import struct_pb2

from core.infrastructure.rpc.gateway_data import decode_feedback
from industrial_gateway.application.point_store import PointStore

METHOD = "WriteMany"
SERVICE = "wind_hub.gateway.v1.GatewayDataService"


class GatewayDataService:
    def __init__(self, store: PointStore) -> None:
        self._store = store

    async def write_many(
        self, request: struct_pb2.Struct, context: grpc.aio.ServicerContext
    ) -> struct_pb2.Struct:
        try:
            values = decode_feedback(request)
            await self._store.update_batch(values, source="grpc")
        except (KeyError, TypeError, ValueError, PermissionError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("gRPC abort unexpectedly returned") from exc
        result = struct_pb2.Struct()
        result.update({"updated": len(values)})
        return result


def create_grpc_server(store: PointStore, bind: str) -> grpc.aio.Server:
    """缺少身份认证时只允许本机监听。"""
    if not (bind.startswith("127.0.0.1:") or bind.startswith("[::1]:")):
        raise ValueError("Gateway data gRPC must bind loopback")
    server = grpc.aio.server()
    handler = grpc.unary_unary_rpc_method_handler(
        GatewayDataService(store).write_many,
        request_deserializer=struct_pb2.Struct.FromString,
        response_serializer=struct_pb2.Struct.SerializeToString,
    )
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(
        SERVICE, {METHOD: handler}
    ),))
    if server.add_insecure_port(bind) == 0:
        raise RuntimeError("unable to bind Gateway data gRPC")
    return server
