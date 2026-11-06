"""gRPC aio 客户端四种调用形态的透明 Request-ID / W3C Trace 传播。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import grpc

from core.infrastructure.diagnostic.correlation import (
    REQUEST_ID_HEADER,
    get_request_id,
    new_request_id,
)


@dataclass(frozen=True, slots=True)
class _CallDetails:
    method: bytes
    timeout: float | None
    metadata: Any
    credentials: grpc.CallCredentials | None
    wait_for_ready: bool | None


def _inject(details: grpc.aio.ClientCallDetails, *, trace_enabled: bool) -> _CallDetails:
    existing = tuple(details.metadata or ())
    metadata = [(key, value) for key, value in existing if key.lower() != REQUEST_ID_HEADER]
    metadata.append((REQUEST_ID_HEADER, get_request_id() or new_request_id()))
    if trace_enabled:
        try:
            from opentelemetry.propagate import inject
        except ImportError as exc:
            raise RuntimeError("Trace propagation requires opentelemetry-api") from exc
        carrier: dict[str, str] = {}
        inject(carrier)
        propagated = {"traceparent", "tracestate", "baggage"}
        metadata = [(k, v) for k, v in metadata if k.lower() not in propagated]
        metadata.extend(carrier.items())
    return _CallDetails(
        method=details.method,
        timeout=details.timeout,
        metadata=tuple(metadata),
        credentials=details.credentials,
        wait_for_ready=details.wait_for_ready,
    )


class RequestIdUnaryUnaryClientInterceptor(
    grpc.aio.UnaryUnaryClientInterceptor  # type: ignore[misc]
):
    def __init__(self, *, tracing: bool = False) -> None:
        self._tracing = tracing

    async def intercept_unary_unary(
        self, continuation: Any, client_call_details: grpc.aio.ClientCallDetails, request: Any
    ) -> Any:
        return await continuation(
            _inject(client_call_details, trace_enabled=self._tracing), request
        )


class RequestIdUnaryStreamClientInterceptor(
    grpc.aio.UnaryStreamClientInterceptor  # type: ignore[misc]
):
    def __init__(self, *, tracing: bool = False) -> None:
        self._tracing = tracing

    async def intercept_unary_stream(
        self, continuation: Any, client_call_details: grpc.aio.ClientCallDetails, request: Any
    ) -> Any:
        return await continuation(
            _inject(client_call_details, trace_enabled=self._tracing), request
        )


class RequestIdStreamUnaryClientInterceptor(
    grpc.aio.StreamUnaryClientInterceptor  # type: ignore[misc]
):
    def __init__(self, *, tracing: bool = False) -> None:
        self._tracing = tracing

    async def intercept_stream_unary(
        self,
        continuation: Any,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        return await continuation(
            _inject(client_call_details, trace_enabled=self._tracing), request_iterator
        )


class RequestIdStreamStreamClientInterceptor(
    grpc.aio.StreamStreamClientInterceptor  # type: ignore[misc]
):
    def __init__(self, *, tracing: bool = False) -> None:
        self._tracing = tracing

    async def intercept_stream_stream(
        self,
        continuation: Any,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        return await continuation(
            _inject(client_call_details, trace_enabled=self._tracing), request_iterator
        )


def client_interceptors(*, tracing: bool = False) -> tuple[
    grpc.aio.UnaryUnaryClientInterceptor
    | grpc.aio.UnaryStreamClientInterceptor
    | grpc.aio.StreamUnaryClientInterceptor
    | grpc.aio.StreamStreamClientInterceptor,
    ...,
]:
    return (
        RequestIdUnaryUnaryClientInterceptor(tracing=tracing),
        RequestIdUnaryStreamClientInterceptor(tracing=tracing),
        RequestIdStreamUnaryClientInterceptor(tracing=tracing),
        RequestIdStreamStreamClientInterceptor(tracing=tracing),
    )
