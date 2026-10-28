"""Web Server outbound client for independent Sink Service gRPC."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import grpc
from google.protobuf import wrappers_pb2


class SinkGrpcClient:
    """Call Sink Service methods over the versioned BytesValue JSON contract."""

    def __init__(self, endpoint: str, timeout: float = 3.0) -> None:
        self._endpoint = endpoint
        self._timeout = timeout

    async def call(self, method: str, name: str | None = None) -> dict[str, Any]:
        """Use an independent channel, never a Collector worker RPC."""
        payload = {"schema_version": 1}
        if name is not None:
            payload["name"] = name
        async with grpc.aio.insecure_channel(self._endpoint) as channel:
            rpc = channel.unary_unary(
                f"/windhub.sink.v1.SinkService/{method}",
                request_serializer=lambda value: value.SerializeToString(),
                response_deserializer=wrappers_pb2.BytesValue.FromString,
            )
            result = await rpc(
                wrappers_pb2.BytesValue(value=json.dumps(payload).encode("utf-8")),
                timeout=self._timeout,
            )
        output = json.loads(result.value)
        if not isinstance(output, dict):
            raise ValueError("Sink Service returned non-object response")
        return output


class SinkGrpcDirectory:
    """Locate independent Sink instances across one or two service processes."""

    def __init__(self) -> None:
        raw = os.environ.get("WIND_HUB_SINK_GRPC_TARGETS", "127.0.0.1:50074")
        targets = [item.strip() for item in raw.split(",") if item.strip()]
        if not targets:
            raise ValueError("WIND_HUB_SINK_GRPC_TARGETS cannot be empty")
        self._clients = [SinkGrpcClient(target) for target in targets]

    async def list_sinks(self) -> list[dict[str, Any]]:
        """Return one consistent set of Sink states, rejecting duplicate ownership."""
        responses = await asyncio.gather(
            *(client.call("ListSinks") for client in self._clients),
            return_exceptions=True,
        )
        rows: dict[str, dict[str, Any]] = {}
        succeeded = False
        for result in responses:
            if isinstance(result, BaseException):
                continue
            succeeded = True
            for item in result.get("items", []):
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise ValueError("invalid Sink status response")
                name = item["name"]
                if name in rows:
                    raise ValueError(f"Sink {name} is owned by multiple service processes")
                rows[name] = item
        if not succeeded:
            raise RuntimeError("all Sink Service endpoints are unavailable")
        return list(rows.values())

    async def call(self, method: str, name: str) -> dict[str, Any]:
        """Route one Sink request to exactly one process."""
        owners: list[SinkGrpcClient] = []
        for client in self._clients:
            try:
                response = await client.call("ListSinks")
            except grpc.RpcError:
                continue
            if any(item.get("name") == name for item in response.get("items", [])):
                owners.append(client)
        if len(owners) != 1:
            raise RuntimeError(f"Sink {name} has {len(owners)} available owners")
        return await owners[0].call(method, name)
