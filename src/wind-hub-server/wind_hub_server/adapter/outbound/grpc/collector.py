"""Collector gRPC 出站适配器。"""

from __future__ import annotations

from typing import Any

from wind_hub_core.rpc.collector import (
    CONTROL_SERVICE,
    GET_COLLECTOR_INFO,
    GET_RUNTIME_STATUS,
    GET_METRICS_SNAPSHOT,
    LIST_DEVICES,
    LIST_SINKS,
    LIST_TASKS,
    LIST_TASK_INSTANCES,
    RELOAD_CONFIG,
    PREPARE_CONFIG,
    ACTIVATE_CONFIG,
    ABORT_CONFIG,
    RUNTIME_SERVICE,
    START_ASSIGNED_TASKS,
    START_TASK,
    START_TASK_INSTANCE,
    STOP_ASSIGNED_TASKS,
    STOP_TASK,
    STOP_TASK_INSTANCE,
    VERIFY_SINK,
    WRITE_TEST_SINK,
    rpc_path,
)
from wind_hub_server.adapter.outbound.grpc.common import (
    GrpcClientBase,
    from_struct,
    to_struct,
)


class CollectorGrpcClient(GrpcClientBase):
    """通过 gRPC 查询和控制独立 Collector。"""

    async def config_status(self) -> dict[str, Any]:
        return await self._empty(RUNTIME_SERVICE, GET_COLLECTOR_INFO)

    async def runtime_status(self) -> dict[str, Any]:
        return await self._empty(RUNTIME_SERVICE, GET_RUNTIME_STATUS)

    async def metrics_snapshot(self) -> dict[str, Any]:
        return await self._empty(RUNTIME_SERVICE, GET_METRICS_SNAPSHOT)

    async def list_devices(self) -> list[dict[str, Any]]:
        data = await self._empty(RUNTIME_SERVICE, LIST_DEVICES)
        return list(data.get("items") or [])

    async def list_sinks(self) -> list[dict[str, Any]]:
        data = await self._empty(RUNTIME_SERVICE, LIST_SINKS)
        return list(data.get("items") or [])

    async def verify_sink(self, name: str) -> dict[str, Any]:
        return await self._struct(RUNTIME_SERVICE, VERIFY_SINK, {"name": name})

    async def write_test_sink(self, name: str) -> dict[str, Any]:
        return await self._struct(RUNTIME_SERVICE, WRITE_TEST_SINK, {"name": name})

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
        return await self._empty(
            CONTROL_SERVICE,
            RELOAD_CONFIG,
            timeout=30.0,
        )

    async def prepare_config(
        self,
        revision_id: str,
        config_hash: str,
        *,
        force_reconfigure: bool = False,
    ) -> dict[str, Any]:
        return await self._struct(
            CONTROL_SERVICE,
            PREPARE_CONFIG,
            {
                "revision_id": revision_id,
                "config_hash": config_hash,
                "force_reconfigure": force_reconfigure,
            },
            timeout=30.0,
        )

    async def activate_config(self, revision_id: str) -> dict[str, Any]:
        return await self._struct(
            CONTROL_SERVICE,
            ACTIVATE_CONFIG,
            {"revision_id": revision_id},
            timeout=30.0,
        )

    async def abort_config(self, revision_id: str) -> dict[str, Any]:
        """撤销 Collector 指定 prepared revision。"""
        return await self._struct(
            CONTROL_SERVICE,
            ABORT_CONFIG,
            {"revision_id": revision_id},
            timeout=30.0,
        )

    async def _empty(
        self,
        service: str,
        method: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return from_struct(
            await self.call_empty_struct(
                rpc_path(service, method),
                timeout=timeout,
            )
        )

    async def _struct(
        self,
        service: str,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return from_struct(
            await self.call_struct(
                rpc_path(service, method),
                to_struct(payload),
                timeout=timeout,
            )
        )
