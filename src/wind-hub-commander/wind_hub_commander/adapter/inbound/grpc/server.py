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
from wind_hub_commander.config import load_commander_config
from wind_hub_core.model.errors import CommandError
from wind_hub_core.rpc import commander_io_pb2 as io_pb
from wind_hub_core.rpc.commander_io_codec import (
    command_from_proto,
    command_result_to_proto,
    point_value_to_proto,
)
from wind_hub_core.rpc.commander import (
    COMMANDER_SERVICE,
    GET_STATUS,
    LIST_DEVICES,
    ACTIVATE_CONFIG,
    PREPARE_CONFIG,
    READ_POINT,
    READ_POINTS,
    RELOAD_CONFIG,
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
                "active_revision": self._app.runtime.active_revision,
                "prepared_revision": self._app.runtime.prepared_revision,
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

    async def prepare_config(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """加载本地候选配置并构造 prepared generation，不切换当前运行配置。"""
        data = _request_dict(request)
        try:
            revision_id = _required_string(data, "revision_id")
            candidate = load_commander_config(self._app.config_dir)
            await self._app.runtime.prepare_config(revision_id, candidate)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(
            {
                "success": True,
                "revision_id": revision_id,
            }
        )

    async def activate_config(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """激活指定 prepared revision。"""
        data = _request_dict(request)
        try:
            revision_id = _required_string(data, "revision_id")
            await self._app.runtime.activate_config(revision_id)
            self._app.config = self._app.runtime.config
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(
            {
                "success": True,
                "revision_id": revision_id,
            }
        )

    async def reload_config(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """兼容入口：本地加载后按 prepare → activate 完成切换。"""
        del request
        revision_id = uuid4().hex
        try:
            candidate = load_commander_config(self._app.config_dir)
            await self._app.runtime.reload(
                candidate,
                revision_id=revision_id,
            )
            self._app.config = self._app.runtime.config
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(
            {
                "success": True,
                "revision_id": revision_id,
            }
        )

    async def read_point(
        self,
        request: io_pb.ReadPointRequest,
        context: grpc.aio.ServicerContext,
    ) -> io_pb.PointValueMessage:
        """强类型单点读取。"""
        try:
            if not request.device_id or not request.point_id:
                raise ValueError("device_id and point_id are required")
            value = await self._app.read.read_point(
                request.device_id,
                request.point_id,
            )
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return point_value_to_proto(value)

    async def read_points(
        self,
        request: io_pb.ReadPointsRequest,
        context: grpc.aio.ServicerContext,
    ) -> io_pb.ReadPointsResponse:
        """强类型批量读取。"""
        try:
            if not request.device_id:
                raise ValueError("device_id is required")
            if not request.point_ids:
                raise ValueError("point_ids must not be empty")
            values = await self._app.read.read_points(
                request.device_id,
                list(request.point_ids),
            )
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        response = io_pb.ReadPointsResponse()
        response.values.extend(point_value_to_proto(value) for value in values)
        return response

    async def write_point(
        self,
        request: io_pb.WritePointRequest,
        context: grpc.aio.ServicerContext,
    ) -> io_pb.CommandResultMessage:
        """强类型单点写入。"""
        try:
            if not request.device_id or not request.point_id:
                raise ValueError("device_id and point_id are required")
            command = command_from_proto(request)
            if not command.command_id:
                command.command_id = uuid4().hex
            result = await self._app.command.send(command)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return command_result_to_proto(result)

    async def write_points(
        self,
        request: io_pb.WritePointsRequest,
        context: grpc.aio.ServicerContext,
    ) -> io_pb.WritePointsResponse:
        """强类型批量写入。"""
        try:
            if not request.commands:
                raise ValueError("commands must not be empty")
            commands = [command_from_proto(item) for item in request.commands]
            for command in commands:
                if not command.command_id:
                    command.command_id = uuid4().hex
            results = await self._app.command.send_batch(commands)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        response = io_pb.WritePointsResponse()
        response.results.extend(command_result_to_proto(result) for result in results)
        return response

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
    """构建 Commander service handler；设备 I/O 使用强类型 Protobuf。"""
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
        RELOAD_CONFIG: grpc.unary_unary_rpc_method_handler(
            service.reload_config,
            request_deserializer=empty_pb2.Empty.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
        PREPARE_CONFIG: grpc.unary_unary_rpc_method_handler(
            service.prepare_config,
            request_deserializer=struct_pb2.Struct.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
        ACTIVATE_CONFIG: grpc.unary_unary_rpc_method_handler(
            service.activate_config,
            request_deserializer=struct_pb2.Struct.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
        READ_POINT: grpc.unary_unary_rpc_method_handler(
            service.read_point,
            request_deserializer=io_pb.ReadPointRequest.FromString,
            response_serializer=io_pb.PointValueMessage.SerializeToString,
        ),
        READ_POINTS: grpc.unary_unary_rpc_method_handler(
            service.read_points,
            request_deserializer=io_pb.ReadPointsRequest.FromString,
            response_serializer=io_pb.ReadPointsResponse.SerializeToString,
        ),
        WRITE_POINT: grpc.unary_unary_rpc_method_handler(
            service.write_point,
            request_deserializer=io_pb.WritePointRequest.FromString,
            response_serializer=io_pb.CommandResultMessage.SerializeToString,
        ),
        WRITE_POINTS: grpc.unary_unary_rpc_method_handler(
            service.write_points,
            request_deserializer=io_pb.WritePointsRequest.FromString,
            response_serializer=io_pb.WritePointsResponse.SerializeToString,
        ),
    }
    unary_struct = {
        VERIFY_DEVICE: service.verify_device,
        RESOLVE_POINT: service.resolve_point,
        VERIFY_POINT: service.verify_point,
        VERIFY_POINTS: service.verify_points,
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
