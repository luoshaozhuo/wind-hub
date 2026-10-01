"""Collector gRPC client。

客户端不读取现场 YAML、不创建 Runtime，只调用 Collector 控制面。
"""

from __future__ import annotations

from typing import Any

import grpc
from google.protobuf import empty_pb2, json_format, struct_pb2

from wind_hub_core.rpc.collector import (
    CONTROL_SERVICE,
    GET_COLLECTOR_INFO,
    GET_RUNTIME_STATUS,
    GET_TASK_INSTANCE,
    LIST_DEVICES,
    LIST_TASK_INSTANCES,
    RUNTIME_SERVICE,
    START_ASSIGNED_TASKS,
    START_TASK_INSTANCE,
    STOP_ASSIGNED_TASKS,
    STOP_TASK_INSTANCE,
    WRITE_POINT,
    rpc_path,
)


def _struct(data: dict[str, Any]) -> struct_pb2.Struct:
    message = struct_pb2.Struct()
    json_format.ParseDict(data, message)
    return message


def _dict(message: struct_pb2.Struct) -> dict[str, Any]:
    return json_format.MessageToDict(message)


class CollectorClient:
    """单 Collector 的异步 gRPC client。"""

    def __init__(self, target: str, *, timeout: float = 5.0) -> None:
        self._target = target
        self._timeout = timeout
        self._channel: grpc.aio.Channel | None = None

    async def __aenter__(self) -> "CollectorClient":
        self._channel = grpc.aio.insecure_channel(self._target)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None

    def _empty_rpc(self, service: str, method: str) -> Any:
        channel = self._require_channel()
        return channel.unary_unary(
            rpc_path(service, method),
            request_serializer=empty_pb2.Empty.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )

    def _struct_rpc(self, service: str, method: str) -> Any:
        channel = self._require_channel()
        return channel.unary_unary(
            rpc_path(service, method),
            request_serializer=struct_pb2.Struct.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )

    def _require_channel(self) -> grpc.aio.Channel:
        if self._channel is None:
            raise RuntimeError("CollectorClient must be used as an async context manager")
        return self._channel

    async def _call_empty(self, service: str, method: str) -> dict[str, Any]:
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
        response = await self._struct_rpc(service, method)(
            _struct(payload),
            timeout=self._timeout,
        )
        return _dict(response)

    async def info(self) -> dict[str, Any]:
        return await self._call_empty(RUNTIME_SERVICE, GET_COLLECTOR_INFO)

    async def status(self) -> dict[str, Any]:
        return await self._call_empty(RUNTIME_SERVICE, GET_RUNTIME_STATUS)

    async def devices(self) -> dict[str, Any]:
        return await self._call_empty(RUNTIME_SERVICE, LIST_DEVICES)

    async def tasks(self) -> dict[str, Any]:
        return await self._call_empty(RUNTIME_SERVICE, LIST_TASK_INSTANCES)

    async def task(self, instance_id: str) -> dict[str, Any]:
        return await self._call_struct(
            RUNTIME_SERVICE,
            GET_TASK_INSTANCE,
            {"instance_id": instance_id},
        )

    async def start_task(self, instance_id: str) -> dict[str, Any]:
        return await self._call_struct(
            CONTROL_SERVICE,
            START_TASK_INSTANCE,
            {"instance_id": instance_id},
        )

    async def stop_task(self, instance_id: str) -> dict[str, Any]:
        return await self._call_struct(
            CONTROL_SERVICE,
            STOP_TASK_INSTANCE,
            {"instance_id": instance_id},
        )

    async def start_all(self) -> dict[str, Any]:
        return await self._call_empty(CONTROL_SERVICE, START_ASSIGNED_TASKS)

    async def stop_all(self) -> dict[str, Any]:
        return await self._call_empty(CONTROL_SERVICE, STOP_ASSIGNED_TASKS)

    async def write(
        self,
        device_id: str,
        point_id: str,
        value: Any,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
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
