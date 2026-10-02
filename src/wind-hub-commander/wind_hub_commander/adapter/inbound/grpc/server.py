"""Commander gRPC Server。

提供即时 read/write 与设备诊断 RPC。业务行为委托 CommanderApp 用例，
本模块只负责 wire 参数解析、错误映射和 gRPC 生命周期。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import grpc
from google.protobuf import empty_pb2, json_format, struct_pb2

from wind_hub_commander.assembly import CommanderApp
from wind_hub_core.model.command import Command
from wind_hub_core.model.errors import CommandError
from wind_hub_core.rpc.commander import (
    COMMANDER_SERVICE,
    GET_STATUS,
    LIST_DEVICES,
    READ_POINT,
    READ_POINTS,
    RESOLVE_POINT,
    VERIFY_DEVICE,
    VERIFY_POINT,
    VERIFY_POINTS,
    WRITE_POINT,
    WRITE_POINTS,
)


def _request_dict(request: struct_pb2.Struct) -> dict[str, Any]:
    return dict(json_format.MessageToDict(request, preserving_proto_field_name=True))


def _struct(data: dict[str, Any]) -> struct_pb2.Struct:
    message = struct_pb2.Struct()
    message.update(data)
    return message


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None or not str(value):
        raise ValueError(f"missing required field '{key}'")
    return str(value)


async def _abort(context: grpc.aio.ServicerContext, exc: Exception) -> None:
    if isinstance(exc, CommandError | KeyError):
        await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
    if isinstance(exc, ValueError):
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
    await context.abort(grpc.StatusCode.INTERNAL, str(exc) or type(exc).__name__)


class CommanderService:
    """Commander gRPC 应用服务。"""

    def __init__(self, app: CommanderApp) -> None:
        self._app = app

    async def get_status(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        del request, context
        healthy = sum(1 for device in self._app.runtime.devices.values() if device.health().healthy)
        return _struct(
            {
                "running": True,
                "device_count": len(self._app.runtime.devices),
                "healthy_devices": healthy,
            }
        )

    async def list_devices(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        del request, context
        rows = [
            {
                "device_id": device.device_id,
                "protocol": device.config.protocol,
                "host": device.config.endpoint.host,
                "port": device.config.endpoint.port,
                "enabled": device.enabled,
                "healthy": device.health().healthy,
            }
            for device in self._app.runtime.devices.values()
        ]
        return _struct({"devices": rows})

    async def read_point(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        data = _request_dict(request)
        try:
            value = await self._app.read.read_point(
                _required_string(data, "device_id"),
                _required_string(data, "point_id"),
            )
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(value.model_dump(mode="json"))

    async def read_points(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        data = _request_dict(request)
        try:
            device_id = _required_string(data, "device_id")
            point_ids = data.get("point_ids")
            if not isinstance(point_ids, list) or not point_ids:
                raise ValueError("'point_ids' must be a non-empty list")
            values = await self._app.read.read_points(
                device_id,
                [str(point_id) for point_id in point_ids],
            )
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct({"values": [v.model_dump(mode="json") for v in values]})

    async def write_point(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        data = _request_dict(request)
        try:
            command = self._command_from_dict(data)
            result = await self._app.command.send(command)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(result.model_dump(mode="json"))

    async def write_points(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        data = _request_dict(request)
        try:
            rows = data.get("commands")
            if not isinstance(rows, list) or not rows:
                raise ValueError("'commands' must be a non-empty list")
            commands = [
                self._command_from_dict(dict(row))
                for row in rows
                if isinstance(row, dict)
            ]
            if len(commands) != len(rows):
                raise ValueError("every command must be an object")
            results = await self._app.command.send_batch(commands)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct({"results": [r.model_dump(mode="json") for r in results]})

    async def verify_device(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        data = _request_dict(request)
        try:
            result = await self._app.diagnostic.verify_device(
                _required_string(data, "device_id"),
                timeout=float(data.get("timeout", 1.0)),
            )
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(result.model_dump(mode="json"))

    async def resolve_point(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        return await self._point_diagnostic(request, context, "resolve")

    async def verify_point(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        return await self._point_diagnostic(request, context, "verify")

    async def verify_points(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        data = _request_dict(request)
        try:
            group = data.get("point_group")
            result = await self._app.diagnostic.verify_points(
                _required_string(data, "device_id"),
                point_group=str(group) if group not in (None, "") else None,
            )
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(result.model_dump(mode="json"))

    async def _point_diagnostic(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
        mode: str,
    ) -> struct_pb2.Struct:
        data = _request_dict(request)
        try:
            device_id = _required_string(data, "device_id")
            point_id = _required_string(data, "point_id")
            if mode == "resolve":
                result = await self._app.diagnostic.resolve_point(device_id, point_id)
            else:
                result = await self._app.diagnostic.verify_point(device_id, point_id)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(result.model_dump(mode="json"))

    @staticmethod
    def _command_from_dict(data: dict[str, Any]) -> Command:
        return Command(
            command_id=str(data.get("command_id") or uuid4().hex),
            device_id=_required_string(data, "device_id"),
            point_id=_required_string(data, "point_id"),
            value=data.get("value"),
            timeout=float(data.get("timeout", 5.0)),
        )


class CommanderGrpcServer:
    """Commander gRPC Server 生命周期包装。"""

    def __init__(self, server: grpc.aio.Server, endpoint: str) -> None:
        self._server = server
        self.endpoint = endpoint

    async def start(self) -> None:
        await self._server.start()

    async def stop(self, grace: float = 5.0) -> None:
        await self._server.stop(grace)


def _handlers(service: CommanderService) -> grpc.GenericRpcHandler:
    unary_struct = {
        READ_POINT: service.read_point,
        READ_POINTS: service.read_points,
        WRITE_POINT: service.write_point,
        WRITE_POINTS: service.write_points,
        VERIFY_DEVICE: service.verify_device,
        RESOLVE_POINT: service.resolve_point,
        VERIFY_POINT: service.verify_point,
        VERIFY_POINTS: service.verify_points,
    }
    handlers: dict[str, grpc.RpcMethodHandler] = {
        GET_STATUS: grpc.unary_unary_rpc_method_handler(
            service.get_status,
            request_deserializer=empty_pb2.Empty.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
        LIST_DEVICES: grpc.unary_unary_rpc_method_handler(
            service.list_devices,
            request_deserializer=empty_pb2.Empty.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
    }
    for method, callback in unary_struct.items():
        handlers[method] = grpc.unary_unary_rpc_method_handler(
            callback,
            request_deserializer=struct_pb2.Struct.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        )
    return grpc.method_handlers_generic_handler(COMMANDER_SERVICE, handlers)


def build_grpc_server(
    app: CommanderApp,
    *,
    host: str,
    port: int,
) -> CommanderGrpcServer:
    """构建 Commander gRPC Server，但不开始监听。"""
    server = grpc.aio.server()
    server.add_generic_rpc_handlers((_handlers(CommanderService(app)),))
    requested = f"{host}:{port}"
    bound = server.add_insecure_port(requested)
    if bound == 0:
        raise RuntimeError(f"failed to bind Commander gRPC endpoint {requested}")
    return CommanderGrpcServer(server, f"{host}:{bound}")
