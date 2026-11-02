"""Typed service-specific gRPC invocation with bounded read polling."""

from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import AsyncIterator
from typing import Any

import grpc
from google.protobuf.json_format import MessageToDict, ParseDict

from .runtime import Runtime

SERVICE_MODULES = {
    "collector": ("collector", "CollectorService"),
    "commander": ("commander", "CommanderService"),
    "sink": ("sink_service", "SinkService"),
    "sink_realtime": ("sink_service", "SinkService"),
    "sink_reliable": ("sink_service", "SinkService"),
    "gateway": ("industrial_gateway", "IndustrialGatewayService"),
    "data-generator": ("data_generator", "DataGeneratorService"),
}


async def invoke(
    runtime: Runtime,
    service: str,
    method: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Invoke only methods declared in the frozen v1 service descriptor."""
    try:
        module_name, service_name = SERVICE_MODULES[service]
    except KeyError as exc:
        raise ValueError(f"unknown RPC service: {service}") from exc
    module = importlib.import_module(f"contracts.{module_name}_pb2")
    grpc_module = importlib.import_module(f"contracts.{module_name}_pb2_grpc")
    descriptor = module.DESCRIPTOR.services_by_name[service_name]
    if method not in descriptor.methods_by_name:
        raise ValueError(f"unsupported {service} RPC: {method}")
    method_descriptor = descriptor.methods_by_name[method]
    from google.protobuf import message_factory

    request_cls = message_factory.GetMessageClass(method_descriptor.input_type)
    request = ParseDict(values, request_cls(), ignore_unknown_fields=False)
    stub = getattr(grpc_module, f"{service_name}Stub")(runtime.channel(service))
    response = await getattr(stub, method)(request, timeout=runtime.target(service).timeout)
    return MessageToDict(
        response,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )


async def resolve_point_ids(
    runtime: Runtime,
    device_id: str,
    provided: list[str] | None,
) -> list[str]:
    """Resolve omitted points from the remote configuration, with hard page bounds."""
    if provided:
        return list(dict.fromkeys(provided))
    point_ids: list[str] = []
    token = ""
    seen: set[str] = set()
    for _ in range(100):
        page = await invoke(
            runtime,
            "collector",
            "ListPoints",
            {
                "device_id": device_id,
                "page_size": 100,
                "page_token": token,
            },
        )
        for point in page.get("points", []):
            if isinstance(point, dict) and isinstance(point.get("point_id"), str):
                point_ids.append(point["point_id"])
        next_token = page.get("next_page_token", "")
        if not next_token:
            break
        if next_token in seen:
            raise RuntimeError("Collector repeated a pagination token")
        seen.add(next_token)
        token = next_token
    else:
        raise RuntimeError("Collector point listing exceeded 100 pages")
    point_ids = list(dict.fromkeys(point_ids))
    if not point_ids:
        raise ValueError(f"Collector has no configured points for device {device_id}")
    return point_ids


async def poll_latest(
    runtime: Runtime,
    device_id: str,
    point_ids: list[str] | None,
    interval_seconds: float,
    count: int | None,
) -> AsyncIterator[dict[str, Any]]:
    """Non-overlapping polling with drift-free monotonic deadlines."""
    if not 0.2 <= interval_seconds <= 60:
        raise ValueError("poll interval must be in [0.2, 60] seconds")
    if count is not None and not 1 <= count <= 100000:
        raise ValueError("count must be 1..100000")
    selected = await resolve_point_ids(runtime, device_id, point_ids)
    index = 0
    failures = 0
    deadline = time.monotonic()
    while count is None or index < count:
        try:
            response = await invoke(
                runtime,
                "collector",
                "ReadPoints",
                {"device_id": device_id, "point_ids": selected},
            )
            failures = 0
            yield response
            index += 1
            if count is not None and index >= count:
                break
        except grpc.aio.AioRpcError as exc:
            if (
                exc.code() != grpc.StatusCode.UNAVAILABLE
                or failures >= runtime.config().poll_reconnect_attempts
            ):
                raise
            failures += 1
            await asyncio.sleep(min(interval_seconds * failures, 5.0))
        deadline += interval_seconds
        await asyncio.sleep(max(0, deadline - time.monotonic()))
