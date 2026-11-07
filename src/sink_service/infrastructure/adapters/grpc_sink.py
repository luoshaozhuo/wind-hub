"""通用 gRPC 遥测 Sink；仅发送有效工程测量值。"""

from __future__ import annotations

import asyncio
from typing import cast

import grpc

from configuration.sink_schema import GrpcSinkConnection
from contracts import industrial_gateway_pb2 as pb
from contracts import industrial_gateway_pb2_grpc as rpc
from core.application import ConnectionHealth
from core.domain.sample_value import Quality
from core.infrastructure.rpc.scalar_codec import encode_scalar
from sink_service.application.export import SinkReferenceExporter
from sink_service.application.point_value import SinkPointValue
from sink_service.application.sink_contract import (
    GrpcSinkAddress,
    ResolvedSink,
    ResolvedSinkPoint,
)


class GrpcSink:
    def __init__(self, config: ResolvedSink) -> None:
        self._settings = GrpcSinkConnection.model_validate(dict(config.connection))
        self._channel: grpc.aio.Channel | None = None
        self._stub: rpc.IndustrialGatewayServiceAsyncStub | None = None
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
        channel = grpc.aio.insecure_channel(self._settings.endpoint)
        self._channel = channel
        # AsyncStub exists only in the mypy-protobuf .pyi; the string forward
        # reference keeps the async contract without a runtime attribute lookup.
        self._stub = cast(
            "rpc.IndustrialGatewayServiceAsyncStub",
            rpc.IndustrialGatewayServiceStub(channel),
        )

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def write(self, batch: list[SinkPointValue]) -> None:
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
        if self._stub is None:
            raise RuntimeError("GrpcSink is closed")
        grouped: dict[str, list[pb.VariableUpdate]] = {}
        for (gateway_id, variable_id), value in values.items():
            grouped.setdefault(gateway_id, []).append(
                pb.VariableUpdate(
                    variable_id=variable_id,
                    value=encode_scalar(value),
                )
            )
        # Gateway UpdateVariables is not an atomic multi-Gateway transaction.
        # Each batch has independent per-variable acknowledgments.
        for gateway_id, updates in grouped.items():
            for start in range(0, len(updates), 500):
                portion = updates[start : start + 500]
                reply = await self._stub.UpdateVariables(
                    pb.UpdateVariablesRequest(
                        gateway_id=gateway_id,
                        values=portion,
                    ),
                    timeout=self._settings.timeout,
                )
                if len(reply.results) != len(portion):
                    raise RuntimeError("Gateway returned incomplete UpdateVariables results")
                for expected, result in zip(portion, reply.results, strict=True):
                    if result.variable_id != expected.variable_id or not result.success:
                        raise RuntimeError(
                            "Gateway variable update failed: "
                            f"{gateway_id}.{expected.variable_id}: "
                            f"{result.error_code} {result.message}"
                        )

    async def write_task(self, task_id: str, batch: list[SinkPointValue]) -> None:
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
