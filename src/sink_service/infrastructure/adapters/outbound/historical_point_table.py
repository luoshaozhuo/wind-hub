"""Configuration Repository adapter for immutable historical PointTable bundles."""

from __future__ import annotations

import asyncio

import grpc

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc
from sink_service.application.historical_point_table import (
    HistoricalPointTable,
    parse_point_table_bundle,
)
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.secrets import resolve_secret


class HistoricalPointTableClient:
    """Load exact source versions; never substitute current recommendations."""

    def __init__(self, endpoint: str, token_ref: str) -> None:
        if not endpoint or not token_ref.startswith("env:"):
            raise ConfigurationError("historical repository endpoint/token are required")
        self._endpoint = endpoint
        self._token_ref = token_ref
        self._cached: dict[tuple[str, str], HistoricalPointTable] = {}
        self._lock = asyncio.Lock()

    async def get(self, point_table_id: str, device_type: str) -> HistoricalPointTable:
        key = (point_table_id, device_type)
        cached = self._cached.get(key)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cached.get(key)
            if cached is not None:
                return cached
            token = resolve_secret(self._token_ref)
            if not token:
                raise ConfigurationError("historical repository token is unavailable")
            async with grpc.aio.insecure_channel(self._endpoint) as channel:
                stub = rpc.ConfigurationRepositoryServiceStub(channel)
                response = await stub.GetPointTableBundle(
                    pb.GetPointTableBundleRequest(point_table_id=point_table_id),
                    metadata=(("authorization", f"Bearer {token}"),),
                    timeout=10.0,
                )
            result = parse_point_table_bundle(
                response, expected_id=point_table_id, expected_type=device_type
            )
            if len(self._cached) >= 128:
                self._cached.clear()
            self._cached[key] = result
            return result
