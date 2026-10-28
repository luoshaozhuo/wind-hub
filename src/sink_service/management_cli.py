"""Standalone CLI for Sink Service management over gRPC."""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import grpc
from google.protobuf import wrappers_pb2


async def request(endpoint: str, operation: str, name: str | None) -> dict[str, Any]:
    """Perform one management RPC without importing Collector or Web Server."""
    body: dict[str, object] = {"schema_version": 1}
    if name is not None:
        body["name"] = name
    async with grpc.aio.insecure_channel(endpoint) as channel:
        call = channel.unary_unary(
            f"/windhub.sink.v1.SinkService/{operation}",
            request_serializer=lambda msg: msg.SerializeToString(),
            response_deserializer=wrappers_pb2.BytesValue.FromString,
        )
        response = await call(
            wrappers_pb2.BytesValue(
                value=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            ),
            timeout=5.0,
        )
    result = json.loads(response.value)
    if not isinstance(result, dict):
        raise RuntimeError("Sink Service returned invalid management response")
    return result


def main() -> None:
    """Invoke Sink Service lifecycle operations from a terminal."""
    parser = argparse.ArgumentParser(prog="wind-hub-sink")
    parser.add_argument("--endpoint", default="127.0.0.1:50074")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "list", "get", "start", "stop", "restart", "clear"):
        subcommand = subparsers.add_parser(command)
        if command in {"get", "start", "stop", "restart", "clear"}:
            subcommand.add_argument("name")
    args = parser.parse_args()
    mapping = {
        "status": "GetServiceStatus",
        "list": "ListSinks",
        "get": "GetSinkStatus",
        "start": "StartSink",
        "stop": "StopSink",
        "restart": "RestartSink",
        "clear": "ClearSink",
    }
    result = asyncio.run(
        request(args.endpoint, mapping[args.command], getattr(args, "name", None)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
