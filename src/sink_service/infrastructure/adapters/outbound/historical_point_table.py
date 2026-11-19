"""Configuration Repository adapter for immutable historical PointTable bundles."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping

import grpc

from contracts import configuration_repository_pb2 as pb
from contracts import configuration_repository_pb2_grpc as rpc
from sink_service.application.business_projection import HistoricalPointTable
from sink_service.infrastructure.adapters.outbound.historical_bundle_codec import (
    parse_point_table_bundle,
)
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.secrets import resolve_secret


class HistoricalPointTableClient:
    """Load exact source versions; never substitute current recommendations."""

    def __init__(
        self, endpoint: str, token_ref: str, approved_manifests: Mapping[str, str]
    ) -> None:
        if not endpoint or not token_ref.startswith("env:"):
            raise ConfigurationError("historical repository endpoint/token are required")
        if not approved_manifests or any(
            re.fullmatch(r"[a-f0-9]{64}", digest) is None
            for digest in approved_manifests.values()
        ):
            raise ConfigurationError("BUSINESS requires approved manifest SHA-256 identities")
        self._approved = dict(approved_manifests)
        self._endpoint = endpoint
        self._token_ref = token_ref
        self._cached: dict[tuple[str, str], HistoricalPointTable] = {}
        self._lock = asyncio.Lock()

    async def get(self, point_table_id: str, device_type: str) -> HistoricalPointTable:
        if point_table_id not in self._approved:
            raise ConfigurationError("PointTable has no approved field mapping evidence")
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
                    pb.GetPointTableBundleRequest(
                        point_table_id=point_table_id,
                        expected_manifest_sha256=self._approved[point_table_id],
                    ),
                    metadata=(("authorization", f"Bearer {token}"),),
                    timeout=10.0,
                )
            if response.manifest_sha256 != self._approved[point_table_id]:
                raise ConfigurationError("unapproved historical PointTable manifest")
            result = parse_point_table_bundle(
                response, expected_id=point_table_id, expected_type=device_type
            )
            if result.manifest_sha256 != self._approved[point_table_id]:
                raise ConfigurationError("historical PointTable evidence changed")
            if len(self._cached) >= 128:
                self._cached.clear()
            self._cached[key] = result
            return result
