"""Authenticated typed gRPC adapter for the current Sink Delivery contract."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import grpc
from google.protobuf.json_format import MessageToDict

from contracts import common_pb2, sink_service_pb2, sink_service_pb2_grpc


class SinkGrpcClient:
    """One independently authenticated Sink Service endpoint."""

    def __init__(self, endpoint: str, timeout: float = 3.0) -> None:
        self._endpoint = endpoint
        self._timeout = timeout

    @staticmethod
    def _credentials() -> grpc.ChannelCredentials:
        names = (
            "WIND_HUB_SINK_CA_CERT",
            "WIND_HUB_SINK_CLIENT_CERT",
            "WIND_HUB_SINK_CLIENT_KEY",
        )
        paths = tuple(os.environ.get(name) for name in names)
        if any(not path for path in paths):
            raise PermissionError("Sink mTLS client credentials are required")
        ca, cert, key = (Path(path) for path in paths if path is not None)
        return grpc.ssl_channel_credentials(
            root_certificates=ca.read_bytes(),
            private_key=key.read_bytes(),
            certificate_chain=cert.read_bytes(),
        )

    async def list_deliveries(self) -> list[dict[str, Any]]:
        """Read complete paginated Delivery states from the official protobuf API."""
        rows: list[dict[str, Any]] = []
        token = ""
        seen: set[str] = set()
        async with grpc.aio.secure_channel(self._endpoint, self._credentials()) as channel:
            stub = sink_service_pb2_grpc.SinkServiceStub(channel)
            for _ in range(100):
                response = await stub.ListDeliveries(
                    sink_service_pb2.ListDeliveriesRequest(page_size=500, page_token=token),
                    timeout=self._timeout,
                )
                rows.extend(
                    MessageToDict(
                        item,
                        preserving_proto_field_name=True,
                        always_print_fields_with_no_presence=True,
                    )
                    for item in response.deliveries
                )
                next_token = response.next_page_token
                if not next_token:
                    return rows
                if next_token in seen:
                    raise RuntimeError("Sink repeated a Delivery page token")
                seen.add(next_token)
                token = next_token
        raise RuntimeError("Sink Delivery listing exceeded 100 pages")

    async def control_delivery(
        self, delivery_id: str, revision: str, *, start: bool
    ) -> dict[str, Any]:
        """Persist an optimistic-concurrency-checked operator intent."""
        if not revision:
            raise ValueError("Delivery revision is required for a control operation")
        async with grpc.aio.secure_channel(self._endpoint, self._credentials()) as channel:
            stub = sink_service_pb2_grpc.SinkServiceStub(channel)
            operation = stub.StartDelivery if start else stub.StopDelivery
            result = await operation(
                common_pb2.OperationRequest(
                    id=delivery_id,
                    expected_revision=revision,
                ),
                timeout=self._timeout,
            )
            return MessageToDict(
                result,
                preserving_proto_field_name=True,
                always_print_fields_with_no_presence=True,
            )


class SinkGrpcDirectory:
    """Reject ambiguous Delivery ownership, including unavailable endpoints."""

    def __init__(self) -> None:
        raw = os.environ.get("WIND_HUB_SINK_GRPC_TARGETS", "127.0.0.1:50074")
        targets = [item.strip() for item in raw.split(",") if item.strip()]
        if not targets or len(targets) != len(set(targets)):
            raise ValueError("Sink targets must be unique and non-empty")
        self._clients = [SinkGrpcClient(target) for target in targets]

    async def _owned_deliveries(self) -> dict[str, tuple[SinkGrpcClient, dict[str, Any]]]:
        results = await asyncio.gather(
            *(client.list_deliveries() for client in self._clients),
            return_exceptions=True,
        )
        owners: dict[str, tuple[SinkGrpcClient, dict[str, Any]]] = {}
        for client, result in zip(self._clients, results, strict=True):
            if isinstance(result, BaseException):
                raise RuntimeError("Sink Delivery ownership cannot be verified") from result
            for item in result:
                identifier = item.get("delivery_id")
                if not isinstance(identifier, str) or not identifier:
                    raise ValueError("Sink returned an invalid Delivery identity")
                if identifier in owners:
                    raise RuntimeError(f"Delivery {identifier} has multiple active owners")
                owners[identifier] = (client, item)
        return owners

    async def list_deliveries(self) -> list[dict[str, Any]]:
        owners = await self._owned_deliveries()
        return [item for _, item in owners.values()]

    async def control_delivery(self, delivery_id: str, *, start: bool) -> dict[str, Any]:
        owners = await self._owned_deliveries()
        if delivery_id not in owners:
            raise KeyError(delivery_id)
        client, item = owners[delivery_id]
        outcome = await client.control_delivery(
            delivery_id, str(item.get("revision", "")), start=start
        )
        if outcome.get("accepted") is not True:
            raise RuntimeError(f"Delivery {delivery_id} control not accepted")
        return outcome
