"""grpc.aio 客户端四种流形态：请求关联、Trace 父子传播和准确完成指标。"""

from __future__ import annotations

import asyncio
import time
from collections import namedtuple
from collections.abc import Callable
from typing import Any

import grpc

from core.infrastructure.diagnostic.correlation import (
    REQUEST_ID_HEADER,
    get_request_id,
    new_request_id,
)

_CallDetails = namedtuple(
    "_CallDetails",
    ("method", "timeout", "metadata", "credentials", "wait_for_ready"),
)


def _inject(details: grpc.aio.ClientCallDetails, *, correlation: bool, tracing: bool) -> Any:
    metadata = tuple(details.metadata or ())
    if correlation:
        metadata = tuple(
            (key, value) for key, value in metadata if str(key).lower() != REQUEST_ID_HEADER
        ) + ((REQUEST_ID_HEADER, get_request_id() or new_request_id()),)
    if tracing:
        from opentelemetry.propagate import inject

        carrier: dict[str, str] = {}
        inject(carrier)
        propagated = {"traceparent", "tracestate", "baggage"}
        metadata = tuple(
            (key, value) for key, value in metadata if str(key).lower() not in propagated
        ) + tuple(carrier.items())
    return _CallDetails(
        details.method,
        details.timeout,
        metadata,
        details.credentials,
        details.wait_for_ready,
    )


async def _invoke(
    continuation: Callable[..., Any],
    details: grpc.aio.ClientCallDetails,
    request: Any,
    *,
    correlation: bool,
    recorder: Any | None,
    tracer: Any | None,
) -> Any:
    started = time.monotonic()
    method = (
        details.method.decode("ascii", "replace")
        if isinstance(details.method, bytes)
        else str(details.method)
    )
    span: Any | None = None
    try:
        if tracer is not None:
            from opentelemetry.trace import SpanKind, use_span

            span = tracer.start_span(method, kind=SpanKind.CLIENT)
            with use_span(span, end_on_exit=False):
                call = await continuation(
                    _inject(details, correlation=correlation, tracing=True),
                    request,
                )
        else:
            call = await continuation(
                _inject(details, correlation=correlation, tracing=False),
                request,
            )
    except BaseException as exc:
        if span is not None:
            span.record_exception(exc)
            span.end()
        if recorder is not None:
            recorder.record("grpc_client", method, "failure", time.monotonic() - started)
        raise

    async def finalize(completed: Any) -> None:
        outcome = "unknown"
        try:
            status = await completed.code()
            outcome = "success" if status is grpc.StatusCode.OK else "failure"
        except Exception:
            outcome = "failure"
        finally:
            if recorder is not None:
                recorder.record("grpc_client", method, outcome, time.monotonic() - started)
            if span is not None:
                span.end()

    if hasattr(call, "add_done_callback"):

        def done(completed: Any) -> None:
            try:
                asyncio.get_running_loop().create_task(finalize(completed))
            except RuntimeError:
                if span is not None:
                    span.end()

        call.add_done_callback(done)
    else:
        # 某些 interceptor 已消费响应：不能推断服务器的最终状态。
        if recorder is not None:
            recorder.record("grpc_client", method, "unknown", time.monotonic() - started)
        if span is not None:
            span.end()
    return call


class _ClientObserver:
    def __init__(self, *, correlation: bool, recorder: Any | None, tracer: Any | None) -> None:
        self._correlation = correlation
        self._recorder = recorder
        self._tracer = tracer

    async def _forward(
        self,
        continuation: Callable[..., Any],
        details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        return await _invoke(
            continuation,
            details,
            request,
            correlation=self._correlation,
            recorder=self._recorder,
            tracer=self._tracer,
        )


class RequestIdUnaryUnaryClientInterceptor(_ClientObserver, grpc.aio.UnaryUnaryClientInterceptor):
    async def intercept_unary_unary(
        self, continuation: Any, client_call_details: grpc.aio.ClientCallDetails, request: Any
    ) -> Any:
        return await self._forward(continuation, client_call_details, request)


class RequestIdUnaryStreamClientInterceptor(_ClientObserver, grpc.aio.UnaryStreamClientInterceptor):
    async def intercept_unary_stream(
        self, continuation: Any, client_call_details: grpc.aio.ClientCallDetails, request: Any
    ) -> Any:
        return await self._forward(continuation, client_call_details, request)


class RequestIdStreamUnaryClientInterceptor(_ClientObserver, grpc.aio.StreamUnaryClientInterceptor):
    async def intercept_stream_unary(
        self,
        continuation: Any,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        return await self._forward(continuation, client_call_details, request_iterator)


class RequestIdStreamStreamClientInterceptor(
    _ClientObserver, grpc.aio.StreamStreamClientInterceptor
):
    async def intercept_stream_stream(
        self,
        continuation: Any,
        client_call_details: grpc.aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        return await self._forward(continuation, client_call_details, request_iterator)


def client_interceptors(
    *,
    correlation: bool = True,
    recorder: Any | None = None,
    tracer: Any | None = None,
) -> tuple[
    grpc.aio.UnaryUnaryClientInterceptor
    | grpc.aio.UnaryStreamClientInterceptor
    | grpc.aio.StreamUnaryClientInterceptor
    | grpc.aio.StreamStreamClientInterceptor,
    ...,
]:
    return (
        RequestIdUnaryUnaryClientInterceptor(
            correlation=correlation, recorder=recorder, tracer=tracer
        ),
        RequestIdUnaryStreamClientInterceptor(
            correlation=correlation, recorder=recorder, tracer=tracer
        ),
        RequestIdStreamUnaryClientInterceptor(
            correlation=correlation, recorder=recorder, tracer=tracer
        ),
        RequestIdStreamStreamClientInterceptor(
            correlation=correlation, recorder=recorder, tracer=tracer
        ),
    )
