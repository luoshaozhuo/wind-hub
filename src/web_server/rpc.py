"""Web 层到 Collector / Commander 的只读 RPC 边界。"""

from __future__ import annotations

from dataclasses import dataclass

import grpc
from google.protobuf import empty_pb2, json_format, message

from core.infrastructure.rpc import collector_pb2 as collector_pb
from core.infrastructure.rpc import collector_pb2_grpc as collector_grpc
from core.infrastructure.rpc import commander_pb2 as commander_pb
from core.infrastructure.rpc import commander_pb2_grpc as commander_grpc


def to_payload(response: message.Message) -> dict[str, object]:
    """保留 proto 字段名称；64 位整数按 protobuf JSON 标准序列化。"""
    result: dict[str, object] = json_format.MessageToDict(
        response, preserving_proto_field_name=True
    )
    return result


@dataclass(frozen=True, slots=True)
class RpcSettings:
    """Web Server 上游端点与每次 RPC 的超时。"""

    collector_target: str = "127.0.0.1:50051"
    commander_target: str = "127.0.0.1:50052"
    timeout_seconds: float = 5.0


class RpcGateway:
    """通过单一生命周期管理两个异步 gRPC channel。"""

    def __init__(self, settings: RpcSettings) -> None:
        if settings.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._settings = settings
        self._collector_channel: grpc.aio.Channel | None = None
        self._commander_channel: grpc.aio.Channel | None = None
        self._collector: collector_grpc.CollectorRuntimeServiceStub | None = None
        self._commander: commander_grpc.CommanderServiceStub | None = None

    async def __aenter__(self) -> RpcGateway:
        self._collector_channel = grpc.aio.insecure_channel(self._settings.collector_target)
        self._commander_channel = grpc.aio.insecure_channel(self._settings.commander_target)
        self._collector = collector_grpc.CollectorRuntimeServiceStub(self._collector_channel)
        self._commander = commander_grpc.CommanderServiceStub(self._commander_channel)
        return self

    async def __aexit__(self, *_: object) -> None:
        try:
            if self._collector_channel is not None:
                await self._collector_channel.close()
        finally:
            if self._commander_channel is not None:
                await self._commander_channel.close()
            self._collector_channel = None
            self._commander_channel = None
            self._collector = None
            self._commander = None

    def collector(self) -> collector_grpc.CollectorRuntimeServiceStub:
        if self._collector is None:
            raise RuntimeError("RPC gateway is not started")
        return self._collector

    def commander(self) -> commander_grpc.CommanderServiceStub:
        if self._commander is None:
            raise RuntimeError("RPC gateway is not started")
        return self._commander

    @property
    def timeout(self) -> float:
        return self._settings.timeout_seconds

    async def collector_info(self) -> dict[str, object]:
        return to_payload(await self.collector().GetCollectorInfo(empty_pb2.Empty(), timeout=self.timeout))

    async def collector_status(self) -> dict[str, object]:
        return to_payload(await self.collector().GetRuntimeStatus(empty_pb2.Empty(), timeout=self.timeout))

    async def collector_metrics(self) -> dict[str, object]:
        return to_payload(await self.collector().GetMetricsSnapshot(empty_pb2.Empty(), timeout=self.timeout))

    async def collector_devices(self) -> dict[str, object]:
        return to_payload(await self.collector().ListDevices(empty_pb2.Empty(), timeout=self.timeout))

    async def collector_sinks(self) -> dict[str, object]:
        return to_payload(await self.collector().ListSinks(empty_pb2.Empty(), timeout=self.timeout))

    async def collector_tasks(self) -> dict[str, object]:
        return to_payload(await self.collector().ListTasks(empty_pb2.Empty(), timeout=self.timeout))

    async def collector_instances(self) -> dict[str, object]:
        return to_payload(await self.collector().ListTaskInstances(empty_pb2.Empty(), timeout=self.timeout))

    async def collector_latest_values(self, device_id: str = "") -> dict[str, object]:
        request = collector_pb.LatestValuesRequest(device_id=device_id)
        return to_payload(await self.collector().QueryLatestValues(request, timeout=self.timeout))

    async def commander_status(self) -> dict[str, object]:
        return to_payload(await self.commander().GetStatus(empty_pb2.Empty(), timeout=self.timeout))

    async def commander_devices(self) -> dict[str, object]:
        return to_payload(await self.commander().ListDevices(empty_pb2.Empty(), timeout=self.timeout))

    async def commander_read(self, device_id: str, point_id: str) -> dict[str, object]:
        request = commander_pb.ReadPointRequest(device_id=device_id, point_id=point_id)
        return to_payload(await self.commander().ReadPoint(request, timeout=self.timeout))

    async def commander_verify_device(self, device_id: str) -> dict[str, object]:
        request = commander_pb.VerifyDeviceRequest(device_id=device_id, timeout=self.timeout)
        return to_payload(await self.commander().VerifyDevice(request, timeout=self.timeout))
