"""通用 gRPC 遥测 Sink；仅发送有效工程测量值。"""

from __future__ import annotations

import asyncio

import grpc

from contracts import gateway_pb2 as pb
from contracts import gateway_pb2_grpc as rpc
from core.application import ConnectionHealth
from core.application.sink_contract import GrpcSinkAddress, ResolvedSink, ResolvedSinkPoint
from sink_service.application.export import SinkReferenceExporter
from core.domain.point_value import PointValue
from core.domain.sample_value import Quality
from core.infrastructure.config.sink_schema import GrpcSinkConnection
from core.infrastructure.rpc.scalar_codec import encode_scalar


class GrpcSink:
    def __init__(self, config: ResolvedSink) -> None:
        self._settings = GrpcSinkConnection.model_validate(dict(config.connection))
        self._channel: grpc.aio.Channel | None = None
        self._write_many: rpc.GatewayDataServiceStub | None = None
        if not isinstance(config, ResolvedSink):
            raise ValueError("GrpcSink requires resolved Collector mappings")
        points = [ResolvedSinkPoint.model_validate(dict(item)) for item in config.points]
        self._routes: dict[tuple[str, str], tuple[str, str]] = {}
        self._exporter = SinkReferenceExporter(points)
        for point in points:
            if not isinstance(point.address, GrpcSinkAddress):
                raise ValueError("GrpcSink point requires GrpcSinkAddress")
            key = (point.source.device_id, point.source.point_id)
            if key in self._routes:
                raise ValueError("duplicate gRPC source mapping")
            self._routes[key] = (point.address.device_id, point.address.point_id)
        self._task_sources = config.task_sources or {}

    async def open(self) -> None:
        if self._channel is not None:
            return
        self._channel = grpc.aio.insecure_channel(self._settings.endpoint)
        self._write_many = rpc.GatewayDataServiceStub(self._channel)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._write_many = None

    async def write(self, batch: list[PointValue]) -> None:
        if not batch:
            return
        values: dict[tuple[str, str], bool | int | float] = {}
        for point in self._exporter.export(batch):
            if point.quality is not Quality.GOOD or point.value is None:
                continue
            source = (point.device_id, point.point_id)
            if source not in self._routes:
                continue
            key = self._routes[source]
            if key in values:
                raise ValueError("duplicate feedback point in batch")
            values[key] = point.value
        if not values:
            return
        if self._write_many is None:
            raise RuntimeError("GrpcSink is closed")
        await self._write_many.WriteMany(
            pb.WriteManyRequest(
                points=[
                    pb.FeedbackPoint(
                        device_id=device_id, point_id=point_id, value=encode_scalar(value)
                    )
                    for (device_id, point_id), value in values.items()
                ]
            ),
            timeout=self._settings.timeout,
        )

    async def write_task(self, task_id: str, batch: list[PointValue]) -> None:
        allowed = set(self._task_sources.get(task_id, ()))
        await self.write([point for point in batch if (point.device_id, point.point_id) in allowed])

    async def flush(self) -> None:
        return None

    async def health(self) -> ConnectionHealth:
        if self._channel is None:
            return ConnectionHealth(healthy=False, message="gRPC channel closed")
        try:
            await asyncio.wait_for(self._channel.channel_ready(), timeout=self._settings.timeout)
        except (TimeoutError, grpc.RpcError) as exc:
            return ConnectionHealth(healthy=False, message=str(exc))
        return ConnectionHealth(healthy=True)
