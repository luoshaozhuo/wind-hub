"""gRPC management server using a documented JSON payload codec.

Transport framing is gRPC unary/unary; method names match contracts/sink.proto.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import grpc
from google.protobuf import wrappers_pb2

from sink_service.application.management import SinkManager

Method = Callable[[str], Awaitable[dict[str, object]]]


def _decode(raw: bytes) -> dict[str, Any]:
    wrapper = wrappers_pb2.BytesValue.FromString(raw)
    payload = json.loads(wrapper.value)
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported SinkService schema_version")
    return payload


def _encode(payload: dict[str, object]) -> bytes:
    return wrappers_pb2.BytesValue(
        value=json.dumps(payload, allow_nan=False, ensure_ascii=False).encode("utf-8")
    ).SerializeToString()


def build_management_server(manager: SinkManager, address: str) -> grpc.aio.Server:
    """Bind all management actions to a single isolated gRPC endpoint."""
    server = grpc.aio.server()

    async def invoke(
        method: Callable[[str], Awaitable[dict[str, object]]] | None,
        request: dict[str, Any],
        context: grpc.aio.ServicerContext[Any, Any],
        *,
        service: bool = False,
        listing: bool = False,
    ) -> dict[str, object]:
        try:
            if service:
                return manager.service_status()
            if listing:
                return {"items": manager.list_sinks()}
            name = request.get("name")
            if not isinstance(name, str) or not name:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "name required")
                raise AssertionError("abort did not terminate")
            if method is None:
                return manager.get_sink_status(name)
            return await method(name)
        except KeyError:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown sink")
            raise AssertionError("abort did not terminate")
        except (RuntimeError, ValueError) as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            raise AssertionError("abort did not terminate")

    handlers: dict[str, grpc.RpcMethodHandler] = {}
    methods = {
        "GetServiceStatus": (None, True, False),
        "ListSinks": (None, False, True),
        "GetSinkStatus": (None, False, False),
        "StartSink": (manager.start_sink, False, False),
        "StopSink": (manager.stop_sink, False, False),
        "RestartSink": (manager.restart_sink, False, False),
        "ClearSink": (manager.clear_sink, False, False),
    }
    for name, (action, service, listing) in methods.items():
        async def handle(
            request: dict[str, Any],
            context: grpc.aio.ServicerContext[Any, Any],
            *,
            _action: Method | None = action,
            _service: bool = service,
            _listing: bool = listing,
        ) -> dict[str, object]:
            return await invoke(_action, request, context, service=_service, listing=_listing)

        handlers[name] = grpc.unary_unary_rpc_method_handler(
            handle, request_deserializer=_decode, response_serializer=_encode,
        )
    server.add_generic_rpc_handlers(
        [grpc.method_handlers_generic_handler("windhub.sink.v1.SinkService", handlers)]
    )
    if server.add_insecure_port(address) == 0:
        raise ValueError(f"cannot bind SinkService gRPC endpoint: {address}")
    return server
