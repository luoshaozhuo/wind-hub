"""Collector gRPC 控制面。

本适配器只把低频控制/运行态查询映射到既有 UseCase；采集 PointValue
不经过本服务传输。当前 v1 过渡契约使用 google.protobuf.Struct/Empty，
避免在接口尚未稳定时维护生成代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import grpc
from google.protobuf import empty_pb2, json_format, struct_pb2

from wind_hub.application.runtime.collector_identity import CollectorIdentity
from wind_hub.assembly import AssembledRuntime
from wind_hub.domain.model.command import Command
from wind_hub_core.rpc.collector import (
    CONTROL_SERVICE,
    GET_COLLECTOR_INFO,
    GET_RUNTIME_STATUS,
    GET_TASK_INSTANCE,
    LIST_DEVICES,
    LIST_TASK_INSTANCES,
    READ_POINT,
    RUNTIME_SERVICE,
    START_ASSIGNED_TASKS,
    START_TASK_INSTANCE,
    STOP_ASSIGNED_TASKS,
    STOP_TASK_INSTANCE,
    WRITE_POINT,
)


def _struct(data: dict[str, Any]) -> struct_pb2.Struct:
    """将 JSON 兼容字典转换为 Protobuf Struct。"""
    message = struct_pb2.Struct()
    json_format.ParseDict(data, message)
    return message


def _request_dict(request: struct_pb2.Struct) -> dict[str, Any]:
    """将 Protobuf Struct 转为普通字典。"""
    return json_format.MessageToDict(request)


def _required_string(data: dict[str, Any], field: str) -> str:
    """读取必填字符串字段。

    Raises:
        ValueError: 字段缺失或为空。
    """
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


async def _abort_invalid(context: grpc.aio.ServicerContext, message: str) -> None:
    """终止非法参数请求。"""
    await context.abort(grpc.StatusCode.INVALID_ARGUMENT, message)


@dataclass(slots=True)
class CollectorGrpcServer:
    """Collector gRPC Server 生命周期包装。"""

    server: grpc.aio.Server
    endpoint: str

    async def start(self) -> None:
        """开始监听。"""
        await self.server.start()

    async def stop(self, grace: float = 5.0) -> None:
        """停止接收请求并等待正在执行的 RPC。"""
        await self.server.stop(grace)


class CollectorRuntimeService:
    """Collector 运行态只读查询。"""

    def __init__(
        self,
        runtime: AssembledRuntime,
        identity: CollectorIdentity,
    ) -> None:
        self._runtime = runtime
        self._identity = identity

    async def get_collector_info(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        del request, context
        return _struct(
            {
                "component": "wind-hub-collector",
                "collector_id": self._identity.collector_id,
                "boot_id": self._identity.boot_id,
                "config_hash": self._identity.config_hash,
                "config_revision": self._identity.config_revision,
                "runtime_running": self._runtime.runtime.running,
            }
        )

    async def get_runtime_status(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        del request, context
        status = await self._runtime.query.status()
        return _struct(status.model_dump(mode="json"))

    async def list_task_instances(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        del request, context
        instances = await self._runtime.tasks.list_instances()
        return _struct(
            {
                "items": [
                    item.model_dump(mode="json")
                    for item in instances
                ]
            }
        )

    async def get_task_instance(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        try:
            instance_id = _required_string(_request_dict(request), "instance_id")
            item = await self._runtime.tasks.get_instance(instance_id)
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown task instance: {exc.args[0]}")
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(item.model_dump(mode="json"))

    async def read_point(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """即时读取单个设备点位。"""
        data = _request_dict(request)
        try:
            device_id = _required_string(data, "device_id")
            point_id = _required_string(data, "point_id")
            value = await self._runtime.query.read_point(device_id, point_id)
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        except Exception as exc:
            from wind_hub.domain.model.errors import CommandError, ProtocolError

            if isinstance(exc, CommandError):
                await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            if isinstance(exc, ProtocolError):
                await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
            raise

        return _struct(value.model_dump(mode="json"))

    async def list_devices(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        del request, context
        devices = await self._runtime.query.list_devices()
        return _struct(
            {
                "items": [
                    item.model_dump(mode="json")
                    for item in devices
                ]
            }
        )


class CollectorControlService:
    """Collector 低频运行控制。"""

    def __init__(self, runtime: AssembledRuntime) -> None:
        self._runtime = runtime

    async def start_task_instance(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        return await self._set_task_instance(request, context, start=True)

    async def stop_task_instance(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        return await self._set_task_instance(request, context, start=False)

    async def _set_task_instance(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
        *,
        start: bool,
    ) -> struct_pb2.Struct:
        try:
            instance_id = _required_string(_request_dict(request), "instance_id")
            operation = (
                self._runtime.tasks.start_instance
                if start
                else self._runtime.tasks.stop_instance
            )
            item = await operation(instance_id)
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown task instance: {exc.args[0]}")
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(item.model_dump(mode="json"))

    async def start_assigned_tasks(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        del request, context
        result = await self._runtime.tasks.start_all_instances()
        return _struct(result.model_dump(mode="json"))

    async def stop_assigned_tasks(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        del request, context
        result = await self._runtime.tasks.stop_all_instances()
        return _struct(result.model_dump(mode="json"))

    async def write_point(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        data = _request_dict(request)
        try:
            device_id = _required_string(data, "device_id")
            point_id = _required_string(data, "point_id")
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        if "value" not in data:
            await _abort_invalid(context, "value is required")
            raise AssertionError("context.abort must terminate the RPC")

        command = Command(
            command_id=str(data.get("command_id") or uuid4()),
            device_id=device_id,
            point_id=point_id,
            value=data["value"],
            timeout=float(data.get("timeout", 5.0)),
        )
        result = await self._runtime.command.send(command)
        return _struct(result.model_dump(mode="json"))


def _runtime_handlers(service: CollectorRuntimeService) -> grpc.GenericRpcHandler:
    """构建 Runtime service generic handler。"""
    return grpc.method_handlers_generic_handler(
        RUNTIME_SERVICE,
        {
            GET_COLLECTOR_INFO: grpc.unary_unary_rpc_method_handler(
                service.get_collector_info,
                request_deserializer=empty_pb2.Empty.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            GET_RUNTIME_STATUS: grpc.unary_unary_rpc_method_handler(
                service.get_runtime_status,
                request_deserializer=empty_pb2.Empty.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            LIST_TASK_INSTANCES: grpc.unary_unary_rpc_method_handler(
                service.list_task_instances,
                request_deserializer=empty_pb2.Empty.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            GET_TASK_INSTANCE: grpc.unary_unary_rpc_method_handler(
                service.get_task_instance,
                request_deserializer=struct_pb2.Struct.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            READ_POINT: grpc.unary_unary_rpc_method_handler(
                service.read_point,
                request_deserializer=struct_pb2.Struct.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            LIST_DEVICES: grpc.unary_unary_rpc_method_handler(
                service.list_devices,
                request_deserializer=empty_pb2.Empty.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
        },
    )


def _control_handlers(service: CollectorControlService) -> grpc.GenericRpcHandler:
    """构建 Control service generic handler。"""
    return grpc.method_handlers_generic_handler(
        CONTROL_SERVICE,
        {
            START_TASK_INSTANCE: grpc.unary_unary_rpc_method_handler(
                service.start_task_instance,
                request_deserializer=struct_pb2.Struct.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            STOP_TASK_INSTANCE: grpc.unary_unary_rpc_method_handler(
                service.stop_task_instance,
                request_deserializer=struct_pb2.Struct.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            START_ASSIGNED_TASKS: grpc.unary_unary_rpc_method_handler(
                service.start_assigned_tasks,
                request_deserializer=empty_pb2.Empty.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            STOP_ASSIGNED_TASKS: grpc.unary_unary_rpc_method_handler(
                service.stop_assigned_tasks,
                request_deserializer=empty_pb2.Empty.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            WRITE_POINT: grpc.unary_unary_rpc_method_handler(
                service.write_point,
                request_deserializer=struct_pb2.Struct.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
        },
    )


def build_grpc_server(
    runtime: AssembledRuntime,
    identity: CollectorIdentity,
    *,
    host: str,
    port: int,
) -> CollectorGrpcServer:
    """构建 Collector gRPC Server，但不开始监听。

    Args:
        runtime: 已装配的 Collector Runtime。
        identity: 本次 Collector 进程身份。
        host: gRPC 监听地址。
        port: gRPC 监听端口。

    Returns:
        可由 Collector 进程统一管理生命周期的 Server 包装。
    """
    server = grpc.aio.server()
    runtime_service = CollectorRuntimeService(runtime, identity)
    control_service = CollectorControlService(runtime)
    server.add_generic_rpc_handlers(
        (
            _runtime_handlers(runtime_service),
            _control_handlers(control_service),
        )
    )
    requested_endpoint = f"{host}:{port}"
    bound_port = server.add_insecure_port(requested_endpoint)
    if bound_port == 0:
        raise RuntimeError(
            f"failed to bind Collector gRPC endpoint {requested_endpoint}"
        )
    endpoint = f"{host}:{bound_port}"
    return CollectorGrpcServer(server=server, endpoint=endpoint)
