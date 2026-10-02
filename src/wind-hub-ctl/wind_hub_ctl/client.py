"""wind-hub-ctl 的 Collector gRPC 客户端。

本模块只使用 collector.proto 生成的 Stub/Message，不读取现场 YAML、不创建
Runtime、不直接访问 PLC。资源生命周期由异步上下文管理器负责。
"""

from __future__ import annotations

from typing import Any

import grpc
from google.protobuf import empty_pb2

from wind_hub_core.rpc import collector_pb2 as pb
from wind_hub_core.rpc import collector_pb2_grpc as pb_grpc
from wind_hub_core.rpc.collector_codec import (
    collector_info_to_dict,
    device_info_to_dict,
    runtime_status_to_dict,
    task_instance_to_dict,
    task_summary_to_dict,
)


class CollectorClient:
    """单个 Collector 的异步 gRPC 只读诊断客户端。"""

    def __init__(self, target: str, *, timeout: float = 5.0) -> None:
        self._target = target
        self._timeout = timeout
        self._channel: grpc.aio.Channel | None = None
        self._runtime_stub: pb_grpc.CollectorRuntimeServiceStub | None = None

    async def __aenter__(self) -> "CollectorClient":
        """创建异步 channel 与 generated stubs。"""
        self._channel = grpc.aio.insecure_channel(self._target)
        self._runtime_stub = pb_grpc.CollectorRuntimeServiceStub(self._channel)
        return self

    async def __aexit__(self, *_: object) -> None:
        """关闭 channel，并清空 stubs。"""
        if self._channel is not None:
            await self._channel.close()
        self._channel = None
        self._runtime_stub = None

    def _runtime(self) -> pb_grpc.CollectorRuntimeServiceStub:
        """返回已进入生命周期的 Runtime stub。"""
        if self._runtime_stub is None:
            raise RuntimeError("CollectorClient must be used as an async context manager")
        return self._runtime_stub

    async def info(self) -> dict[str, Any]:
        """查询 Collector 进程身份、配置指纹和 Runtime 状态。"""
        response = await self._runtime().GetCollectorInfo(
            empty_pb2.Empty(),
            timeout=self._timeout,
        )
        return collector_info_to_dict(response)

    async def status(self) -> dict[str, Any]:
        """查询 Collector Runtime 聚合状态。"""
        response = await self._runtime().GetRuntimeStatus(
            empty_pb2.Empty(),
            timeout=self._timeout,
        )
        return runtime_status_to_dict(response)

    async def devices(self) -> dict[str, Any]:
        """列出当前 Runtime 注册设备。"""
        response = await self._runtime().ListDevices(
            empty_pb2.Empty(),
            timeout=self._timeout,
        )
        return {"items": [device_info_to_dict(item) for item in response.items]}

    async def tasks(self) -> dict[str, Any]:
        """列出 Task Definition 聚合状态。"""
        response = await self._runtime().ListTasks(
            empty_pb2.Empty(),
            timeout=self._timeout,
        )
        return {"items": [task_summary_to_dict(item) for item in response.items]}

    async def task(self, task_id: str) -> dict[str, Any]:
        """按 task_id 查询 Task 聚合状态。"""
        response = await self._runtime().GetTask(
            pb.TaskIdRequest(task_id=task_id),
            timeout=self._timeout,
        )
        return task_summary_to_dict(response)

    async def task_instances(self) -> dict[str, Any]:
        """列出当前展开的全部 Task Instance。"""
        response = await self._runtime().ListTaskInstances(
            empty_pb2.Empty(),
            timeout=self._timeout,
        )
        return {"items": [task_instance_to_dict(item) for item in response.items]}

    async def task_instance(self, instance_id: str) -> dict[str, Any]:
        """按 instance_id 查询一个 Task Instance。"""
        response = await self._runtime().GetTaskInstance(
            pb.InstanceIdRequest(instance_id=instance_id),
            timeout=self._timeout,
        )
        return task_instance_to_dict(response)

