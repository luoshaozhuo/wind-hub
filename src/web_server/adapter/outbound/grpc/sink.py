"""Web Server outbound client for independent Sink Service gRPC."""
from __future__ import annotations

import json
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
