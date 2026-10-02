"""wind-hub-ctl 的 Collector gRPC 客户端。

本模块位于控制面客户端边界，只负责把 CLI 请求转换为 Collector gRPC 调用；
不读取现场 YAML、不创建 Runtime、不直接访问 PLC，也不参与任务调度。

当前 v1 wire contract 使用 google.protobuf.Struct，因此响应在反序列化后以
dict[str, Any] 表达。这里的 Any 仅用于 Protobuf 动态 JSON 边界，不向
Collector 内部领域模型扩散。

资源生命周期由 CollectorClient 异步上下文管理器负责：进入上下文创建
channel，退出时关闭 channel。单次 RPC 超时由 timeout 统一控制。
"""

from __future__ import annotations

from typing import Any

import grpc
from google.protobuf import empty_pb2, json_format, struct_pb2

from wind_hub_core.rpc.collector import (
    CONTROL_SERVICE,
    DIAGNOSTIC_SERVICE,
    GET_COLLECTOR_INFO,
    GET_RUNTIME_STATUS,
    GET_TASK,
    GET_TASK_INSTANCE,
    LIST_DEVICES,
    LIST_TASKS,
    LIST_TASK_INSTANCES,
    READ_POINT,
    RESOLVE_POINT,
    RELOAD_CONFIG,
    VERIFY_DEVICE,
    VERIFY_POINT,
    VERIFY_POINTS,
    RUNTIME_SERVICE,
    START_ASSIGNED_TASKS,
    START_TASK,
    START_TASK_INSTANCE,
    STOP_ASSIGNED_TASKS,
    STOP_TASK,
    STOP_TASK_INSTANCE,
    WRITE_POINT,
    rpc_path,
)


def _struct(data: dict[str, Any]) -> struct_pb2.Struct:
    """把动态 JSON 字典转换为 Protobuf Struct。

    Any 仅对应 Protobuf Struct 允许的动态 JSON 值；业务层不会依赖该类型。
    """
    message = struct_pb2.Struct()
    json_format.ParseDict(data, message)
    return message


def _dict(message: struct_pb2.Struct) -> dict[str, Any]:
    """把 Protobuf Struct 转换为 CLI 可直接序列化的字典。"""
    return json_format.MessageToDict(message)


class CollectorClient:
    """单个 Collector 的异步 gRPC 控制客户端。

    Args:
        target: Collector gRPC endpoint，例如 127.0.0.1:50051。
        timeout: 单次 RPC 的客户端超时，单位秒。

    Notes:
        实例必须通过 async with 使用，确保 channel 在退出路径上被关闭。
        本类不实现自动重试；连接失败、超时和服务端状态码由 grpc.aio
        原样抛给上层 CLI 统一处理。
    """

    def __init__(self, target: str, *, timeout: float = 5.0) -> None:
        self._target = target
        self._timeout = timeout
        self._channel: grpc.aio.Channel | None = None

    async def __aenter__(self) -> "CollectorClient":
        """创建异步 channel 并返回当前客户端。

        Returns:
            已进入连接生命周期的客户端实例。
        """
        self._channel = grpc.aio.insecure_channel(self._target)
        return self

    async def __aexit__(self, *_: object) -> None:
        """关闭当前 channel；重复退出不会重复释放资源。"""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None

    def _empty_rpc(self, service: str, method: str) -> Any:
        """构造 Empty 请求的 unary-unary 动态 stub。

        grpc.aio.Channel.unary_unary 的动态返回类型缺少可用静态类型信息，
        因此该第三方边界保留 Any；返回值只在本模块内部调用。
        """
        channel = self._require_channel()
        return channel.unary_unary(
            rpc_path(service, method),
            request_serializer=empty_pb2.Empty.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )

    def _struct_rpc(self, service: str, method: str) -> Any:
        """构造 Struct 请求的 unary-unary 动态 stub。

        Any 仅隔离 gRPC 动态 stub 类型，不作为公开业务数据模型。
        """
        channel = self._require_channel()
        return channel.unary_unary(
            rpc_path(service, method),
            request_serializer=struct_pb2.Struct.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )

    def _require_channel(self) -> grpc.aio.Channel:
        """返回已建立的 channel。

        Raises:
            RuntimeError: 客户端未通过异步上下文管理器进入连接生命周期。
        """
        if self._channel is None:
            raise RuntimeError("CollectorClient must be used as an async context manager")
        return self._channel

    async def _call_empty(self, service: str, method: str) -> dict[str, Any]:
        """执行 Empty 请求 RPC，并把动态响应转换为字典。"""
        response = await self._empty_rpc(service, method)(
            empty_pb2.Empty(),
            timeout=self._timeout,
        )
        return _dict(response)

    async def _call_struct(
        self,
        service: str,
        method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """执行 Struct 请求 RPC，并把动态响应转换为字典。"""
        response = await self._struct_rpc(service, method)(
            _struct(payload),
            timeout=self._timeout,
        )
        return _dict(response)

    async def info(self) -> dict[str, Any]:
        """查询 Collector 进程身份、配置指纹和 Runtime 运行状态。

        Returns:
            CollectorInfo 的动态 JSON 字典。
        """
        return await self._call_empty(RUNTIME_SERVICE, GET_COLLECTOR_INFO)

    async def status(self) -> dict[str, Any]:
        """查询 Collector Runtime 的聚合运行状态。

        Returns:
            Runtime 状态的动态 JSON 字典。
        """
        return await self._call_empty(RUNTIME_SERVICE, GET_RUNTIME_STATUS)

    async def devices(self) -> dict[str, Any]:
        """列出当前 Runtime 注册的设备及其连接状态。

        Returns:
            包含设备列表的动态 JSON 字典。
        """
        return await self._call_empty(RUNTIME_SERVICE, LIST_DEVICES)

    async def read(self, device_id: str, point_id: str) -> dict[str, Any]:
        """即时读取单个设备点位，绕过周期采集缓存。

        Args:
            device_id: Runtime 中的设备标识。
            point_id: 设备点表中的点标识。

        Returns:
            Collector 返回的 PointValue JSON 字典。
        """
        return await self._call_struct(
            RUNTIME_SERVICE,
            READ_POINT,
            {"device_id": device_id, "point_id": point_id},
        )

    async def tasks(self) -> dict[str, Any]:
        """列出 Task Definition 及其聚合运行状态。

        Returns:
            包含 Task 列表的动态 JSON 字典。
        """
        return await self._call_empty(RUNTIME_SERVICE, LIST_TASKS)

    async def task(self, task_id: str) -> dict[str, Any]:
        """按稳定 task_id 查询一个 Task 的聚合状态。

        Args:
            task_id: Task Definition 的稳定标识。

        Returns:
            Task 聚合状态的动态 JSON 字典。
        """
        return await self._call_struct(
            RUNTIME_SERVICE,
            GET_TASK,
            {"task_id": task_id},
        )

    async def task_instances(self) -> dict[str, Any]:
        """列出当前展开的全部 Task Instance。

        Returns:
            包含 Task Instance 列表的动态 JSON 字典。
        """
        return await self._call_empty(RUNTIME_SERVICE, LIST_TASK_INSTANCES)

    async def task_instance(self, instance_id: str) -> dict[str, Any]:
        """按 instance_id 查询一个 Task Instance。

        Args:
            instance_id: Task Instance 的稳定标识。

        Returns:
            Task Instance 状态的动态 JSON 字典。
        """
        return await self._call_struct(
            RUNTIME_SERVICE,
            GET_TASK_INSTANCE,
            {"instance_id": instance_id},
        )

    async def start_task(self, task_id: str) -> dict[str, Any]:
        """按 task_id 启动该 Task 当前展开的全部实例。

        Args:
            task_id: Task Definition 的稳定标识。

        Returns:
            启动后的 Task 聚合状态。
        """
        return await self._call_struct(
            CONTROL_SERVICE,
            START_TASK,
            {"task_id": task_id},
        )

    async def stop_task(self, task_id: str) -> dict[str, Any]:
        """按 task_id 停止该 Task 当前展开的全部实例。

        Args:
            task_id: Task Definition 的稳定标识。

        Returns:
            停止后的 Task 聚合状态。
        """
        return await self._call_struct(
            CONTROL_SERVICE,
            STOP_TASK,
            {"task_id": task_id},
        )

    async def start_task_instance(self, instance_id: str) -> dict[str, Any]:
        """启动单个 Task Instance，不影响同 Task 的其他实例。

        Args:
            instance_id: 待启动的 Task Instance 标识。

        Returns:
            启动后的 Task Instance 状态。
        """
        return await self._call_struct(
            CONTROL_SERVICE,
            START_TASK_INSTANCE,
            {"instance_id": instance_id},
        )

    async def stop_task_instance(self, instance_id: str) -> dict[str, Any]:
        """停止单个 Task Instance，不删除实例定义。

        Args:
            instance_id: 待停止的 Task Instance 标识。

        Returns:
            停止后的 Task Instance 状态。
        """
        return await self._call_struct(
            CONTROL_SERVICE,
            STOP_TASK_INSTANCE,
            {"instance_id": instance_id},
        )

    async def start_all(self) -> dict[str, Any]:
        """启动当前 Collector 已分配的全部 Task Instance。

        Returns:
            批量启动结果的动态 JSON 字典。
        """
        return await self._call_empty(CONTROL_SERVICE, START_ASSIGNED_TASKS)

    async def stop_all(self) -> dict[str, Any]:
        """停止当前 Collector 已分配的全部 Task Instance。

        Returns:
            批量停止结果的动态 JSON 字典。
        """
        return await self._call_empty(CONTROL_SERVICE, STOP_ASSIGNED_TASKS)

    async def reload(self) -> dict[str, Any]:
        """触发 Collector 从本地配置目录执行一次增量热重载。

        Returns:
            ReloadResult 的动态 JSON 字典，包含成功状态、diff 和错误列表。
        """
        return await self._call_empty(CONTROL_SERVICE, RELOAD_CONFIG)


    async def verify_device(
        self,
        device_id: str,
        *,
        timeout: float = 1.0,
    ) -> dict[str, Any]:
        """验证设备网络、TCP 与协议会话。"""
        return await self._call_struct(
            DIAGNOSTIC_SERVICE,
            VERIFY_DEVICE,
            {"device_id": device_id, "timeout": timeout},
        )

    async def resolve_point(self, device_id: str, point_id: str) -> dict[str, Any]:
        """解析点位协议地址；ADS 返回实际 index_group/index_offset。"""
        return await self._call_struct(
            DIAGNOSTIC_SERVICE,
            RESOLVE_POINT,
            {"device_id": device_id, "point_id": point_id},
        )

    async def verify_point(self, device_id: str, point_id: str) -> dict[str, Any]:
        """实际读取单点并返回 raw/engineering value。"""
        return await self._call_struct(
            DIAGNOSTIC_SERVICE,
            VERIFY_POINT,
            {"device_id": device_id, "point_id": point_id},
        )

    async def verify_points(
        self,
        device_id: str,
        *,
        point_group: str | None = None,
    ) -> dict[str, Any]:
        """验证整个设备点表或指定 point_group。"""
        payload: dict[str, Any] = {"device_id": device_id}
        if point_group is not None:
            payload["point_group"] = point_group
        return await self._call_struct(
            DIAGNOSTIC_SERVICE,
            VERIFY_POINTS,
            payload,
        )

    async def write(
        self,
        device_id: str,
        point_id: str,
        value: Any,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """向单个设备点位下发写指令。

        Args:
            device_id: 目标设备标识。
            point_id: 目标点标识。
            value: CLI 解析后的 JSON 标量或字符串；实际类型由点表约束。
            timeout: 设备写操作超时，单位秒。

        Returns:
            CommandResult 的 JSON 字典。

        Notes:
            value 使用 Any 是 CLI/Protobuf 动态输入边界；Collector 内部仍由
            点表和协议驱动负责类型约束。
        """
        return await self._call_struct(
            CONTROL_SERVICE,
            WRITE_POINT,
            {
                "device_id": device_id,
                "point_id": point_id,
                "value": value,
                "timeout": timeout,
            },
        )
