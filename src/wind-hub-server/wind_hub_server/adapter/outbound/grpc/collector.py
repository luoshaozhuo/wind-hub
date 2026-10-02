"""Collector gRPC 出站适配器。"""

from __future__ import annotations

from typing import Any

from google.protobuf import empty_pb2

from wind_hub_core.rpc.collector import (
    CONTROL_SERVICE,
    GET_RUNTIME_STATUS,
    LIST_DEVICES,
    LIST_TASKS,
    LIST_TASK_INSTANCES,
    RELOAD_CONFIG,
    RUNTIME_SERVICE,
    START_ASSIGNED_TASKS,
    START_TASK,
    START_TASK_INSTANCE,
    STOP_ASSIGNED_TASKS,
    STOP_TASK,
    STOP_TASK_INSTANCE,
    rpc_path,
)
from wind_hub_server.adapter.outbound.grpc.common import (
    GrpcClientBase,
    from_struct,
    to_struct,
)


class CollectorGrpcClient(GrpcClientBase):
    """通过 gRPC 查询和控制独立 Collector。"""

    async def runtime_status(self) -> dict[str, Any]:
        return await self._empty(RUNTIME_SERVICE, GET_RUNTIME_STATUS)

    async def list_devices(self) -> list[dict[str, Any]]:
        data = await self._empty(RUNTIME_SERVICE, LIST_DEVICES)
        return list(data.get("items") or [])

    async def list_tasks(self) -> list[dict[str, Any]]:
        data = await self._empty(RUNTIME_SERVICE, LIST_TASKS)
        return list(data.get("items") or [])

    async def list_task_instances(self) -> list[dict[str, Any]]:
        data = await self._empty(RUNTIME_SERVICE, LIST_TASK_INSTANCES)
        return list(data.get("items") or [])

    async def start_task(self, task_id: str) -> dict[str, Any]:
        return await self._struct(CONTROL_SERVICE, START_TASK, {"task_id": task_id})

    async def stop_task(self, task_id: str) -> dict[str, Any]:
        return await self._struct(CONTROL_SERVICE, STOP_TASK, {"task_id": task_id})

    async def start_task_instance(self, instance_id: str) -> dict[str, Any]:
        return await self._struct(
            CONTROL_SERVICE,
            START_TASK_INSTANCE,
            {"instance_id": instance_id},
        )

    async def stop_task_instance(self, instance_id: str) -> dict[str, Any]:
        return await self._struct(
            CONTROL_SERVICE,
            STOP_TASK_INSTANCE,
            {"instance_id": instance_id},
        )

    async def start_all(self) -> dict[str, Any]:
        return await self._empty(CONTROL_SERVICE, START_ASSIGNED_TASKS)

    async def stop_all(self) -> dict[str, Any]:
        return await self._empty(CONTROL_SERVICE, STOP_ASSIGNED_TASKS)

    async def reload_config(self) -> dict[str, Any]:
        return await self._empty(CONTROL_SERVICE, RELOAD_CONFIG)

    async def _empty(self, service: str, method: str) -> dict[str, Any]:
        call = self.unary_empty_struct(rpc_path(service, method))
        return from_struct(await call(empty_pb2.Empty()))

    async def _struct(
        self,
        service: str,
        method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        call = self.unary_struct(rpc_path(service, method))
        return from_struct(await call(to_struct(payload)))
