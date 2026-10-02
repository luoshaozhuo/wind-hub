"""Collector gRPC 控制面。

本适配器只把低频控制/运行态查询映射到既有 UseCase；采集 PointValue
不经过本服务传输。当前 v1 过渡契约使用 google.protobuf.Struct/Empty，
避免在接口尚未稳定时维护生成代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import grpc
from google.protobuf import empty_pb2, json_format, struct_pb2

from wind_hub.application.runtime.collector_identity import CollectorIdentity
from wind_hub.assembly import AssembledRuntime
from wind_hub_core.rpc.collector import (
    CONTROL_SERVICE,
    GET_COLLECTOR_INFO,
    GET_RUNTIME_STATUS,
    GET_METRICS_SNAPSHOT,
    GET_TASK,
    GET_TASK_INSTANCE,
    LIST_DEVICES,
    LIST_SINKS,
    LIST_TASKS,
    LIST_TASK_INSTANCES,
    RELOAD_CONFIG,
    PREPARE_CONFIG,
    ACTIVATE_CONFIG,
    VERIFY_SINK,
    WRITE_TEST_SINK,
    RUNTIME_SERVICE,
    START_ASSIGNED_TASKS,
    START_TASK,
    START_TASK_INSTANCE,
    STOP_ASSIGNED_TASKS,
    STOP_TASK,
    STOP_TASK_INSTANCE,
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
        """开始监听 gRPC endpoint。

        Raises:
            Exception: gRPC Server 启动失败时原样传播，由 Collector 进程入口处理。
        """
        await self.server.start()

    async def stop(self, grace: float = 5.0) -> None:
        """停止接收新请求并等待正在执行的 RPC。

        Args:
            grace: 已进入处理阶段 RPC 的最大宽限时间，单位秒。
        """
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
        """返回 Collector 身份、配置指纹与 Runtime 运行事实。

        Returns:
            Protobuf Struct 形式的 Collector 基本信息。
        """
        del request, context
        return _struct(
            {
                "component": "wind-hub-collector",
                "collector_id": self._identity.collector_id,
                "boot_id": self._identity.boot_id,
                "config_hash": self._runtime.config.config_hash,
                "boot_config_hash": self._identity.config_hash,
                "config_revision": self._identity.config_revision,
                "active_revision": self._runtime.config.active_revision,
                "prepared_revision": self._runtime.config.prepared_revision,
                "runtime_running": self._runtime.runtime.running,
            }
        )

    async def get_runtime_status(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """返回 Runtime 聚合状态快照。

        Returns:
            Protobuf Struct 形式的 Runtime 状态。
        """
        del request, context
        status = await self._runtime.query.status()
        return _struct(status.model_dump(mode="json"))

    async def get_metrics_snapshot(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """返回 Collector 本地采集质量累计计数与近期事件。"""
        del request, context
        return _struct(self._runtime.metrics_state.snapshot())

    async def list_tasks(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """列出 Task Definition 与聚合运行状态。

        Returns:
            包含 TaskSummary 列表的 Protobuf Struct。
        """
        del request, context
        items = await self._runtime.tasks.list_task_summaries()
        return _struct({"items": [item.model_dump(mode="json") for item in items]})

    async def get_task(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """按稳定 task_id 查询 Task 聚合状态。

        Returns:
            TaskSummary 的 Protobuf Struct 表示。

        Raises:
            grpc.RpcError: task_id 非法或 Task 不存在时通过 context.abort 终止 RPC。
        """
        try:
            task_id = _required_string(_request_dict(request), "task_id")
            item = await self._runtime.tasks.get_task_summary(task_id)
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown task: {exc.args[0]}")
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(item.model_dump(mode="json"))

    async def list_task_instances(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """列出当前展开的全部 Task Instance。

        Returns:
            包含 Task Instance 列表的 Protobuf Struct。
        """
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
        """按 instance_id 查询单个 Task Instance。

        Returns:
            Task Instance 状态的 Protobuf Struct。

        Raises:
            grpc.RpcError: 参数非法或实例不存在时通过 context.abort 终止 RPC。
        """
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

    async def list_sinks(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """列出当前 Runtime Sink 健康状态与队列深度。"""
        del request, context
        health = self._runtime.runtime.health()
        depths = self._runtime.runtime.sink_queue_depths()
        items = []
        for name, sink in self._runtime.runtime.sinks.items():
            current = health.get(name)
            items.append(
                {
                    "name": name,
                    "healthy": bool(current.healthy) if current is not None else False,
                    "message": current.message if current is not None else None,
                    "queue_depth": int(depths.get(name, 0)),
                }
            )
        return _struct({"items": items})

    async def verify_sink(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """返回当前运行 Sink 的真实 health 与队列深度。"""
        data = _request_dict(request)
        try:
            name = _required_string(data, "name")
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        sink = self._runtime.runtime.sinks.get(name)
        if sink is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown sink: {name}")
            raise AssertionError("context.abort must terminate the RPC")
        health = sink.health()
        return _struct(
            {
                "success": bool(health.healthy),
                "message": health.message,
                "queue_depth": int(
                    self._runtime.runtime.sink_queue_depths().get(name, 0)
                ),
            }
        )

    async def write_test_sink(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """向当前运行 Sink 写入一条明确标记的诊断 PointValue。"""
        data = _request_dict(request)
        try:
            name = _required_string(data, "name")
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        sink = self._runtime.runtime.sinks.get(name)
        if sink is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown sink: {name}")
            raise AssertionError("context.abort must terminate the RPC")
        from wind_hub_core.model.point import PointValue

        try:
            await sink.write(
                [PointValue(device_id="_diagnostic", point_id="_write_test", value=1)]
            )
            await sink.flush()
        except Exception as exc:
            return _struct(
                {
                    "success": False,
                    "message": str(exc) or type(exc).__name__,
                }
            )
        return _struct({"success": True, "message": None})

    async def list_devices(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """列出当前 Runtime 注册设备及连接状态。

        Returns:
            包含设备列表的 Protobuf Struct。
        """
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

    async def start_task(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """启动一个 Task 当前展开的全部实例。

        Returns:
            启动后的 Task 聚合状态。
        """
        return await self._set_task(request, context, start=True)

    async def stop_task(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """停止一个 Task 当前展开的全部实例。

        Returns:
            停止后的 Task 聚合状态。
        """
        return await self._set_task(request, context, start=False)

    async def _set_task(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
        *,
        start: bool,
    ) -> struct_pb2.Struct:
        try:
            task_id = _required_string(_request_dict(request), "task_id")
            operation = self._runtime.tasks.start_task if start else self._runtime.tasks.stop_task
            item = await operation(task_id)
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown task: {exc.args[0]}")
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _struct(item.model_dump(mode="json"))

    async def start_task_instance(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """启动单个 Task Instance。

        Returns:
            启动后的 Task Instance 状态。
        """
        return await self._set_task_instance(request, context, start=True)

    async def stop_task_instance(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """停止单个 Task Instance。

        Returns:
            停止后的 Task Instance 状态。
        """
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
        """启动当前 Collector 已分配的全部 Task Instance。

        Returns:
            批量启动结果。
        """
        del request, context
        result = await self._runtime.tasks.start_all_instances()
        return _struct(result.model_dump(mode="json"))

    async def stop_assigned_tasks(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """停止当前 Collector 已分配的全部 Task Instance。

        Returns:
            批量停止结果。
        """
        del request, context
        result = await self._runtime.tasks.stop_all_instances()
        return _struct(result.model_dump(mode="json"))

    async def prepare_config(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """加载并保存候选配置，不修改当前 Runtime。"""
        try:
            data = _request_dict(request)
            revision_id = _required_string(data, "revision_id")
            config_hash = _required_string(data, "config_hash")
            result = await self._runtime.config.prepare_config(
                revision_id,
                expected_config_hash=config_hash,
                force_reconfigure=bool(data.get("force_reconfigure", False)),
            )
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        payload = result.model_dump(mode="json")
        payload["revision_id"] = revision_id
        payload["config_hash"] = self._runtime.config.prepared_hash
        return _struct(payload)

    async def activate_config(
        self,
        request: struct_pb2.Struct,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """激活指定 prepared revision，并执行现有增量 reconfigure。"""
        try:
            revision_id = _required_string(_request_dict(request), "revision_id")
            result = await self._runtime.config.activate_config(revision_id)
        except ValueError as exc:
            await _abort_invalid(context, str(exc))
            raise AssertionError("context.abort must terminate the RPC") from exc
        payload = result.model_dump(mode="json")
        payload["revision_id"] = revision_id
        return _struct(payload)

    async def reload_config(
        self,
        request: empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> struct_pb2.Struct:
        """兼容入口：按 prepare → activate 执行一次增量热重载。"""
        del request, context
        result = await self._runtime.config.reload()
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
            GET_METRICS_SNAPSHOT: grpc.unary_unary_rpc_method_handler(
                service.get_metrics_snapshot,
                request_deserializer=empty_pb2.Empty.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            LIST_TASKS: grpc.unary_unary_rpc_method_handler(
                service.list_tasks,
                request_deserializer=empty_pb2.Empty.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            GET_TASK: grpc.unary_unary_rpc_method_handler(
                service.get_task,
                request_deserializer=struct_pb2.Struct.FromString,
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
            LIST_DEVICES: grpc.unary_unary_rpc_method_handler(
                service.list_devices,
                request_deserializer=empty_pb2.Empty.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            LIST_SINKS: grpc.unary_unary_rpc_method_handler(
                service.list_sinks,
                request_deserializer=empty_pb2.Empty.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            VERIFY_SINK: grpc.unary_unary_rpc_method_handler(
                service.verify_sink,
                request_deserializer=struct_pb2.Struct.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            WRITE_TEST_SINK: grpc.unary_unary_rpc_method_handler(
                service.write_test_sink,
                request_deserializer=struct_pb2.Struct.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
        },
    )


def _control_handlers(service: CollectorControlService) -> grpc.GenericRpcHandler:
    """构建 Control service generic handler。"""
    return grpc.method_handlers_generic_handler(
        CONTROL_SERVICE,
        {
            START_TASK: grpc.unary_unary_rpc_method_handler(
                service.start_task,
                request_deserializer=struct_pb2.Struct.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
            STOP_TASK: grpc.unary_unary_rpc_method_handler(
                service.stop_task,
                request_deserializer=struct_pb2.Struct.FromString,
                response_serializer=struct_pb2.Struct.SerializeToString,
            ),
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
