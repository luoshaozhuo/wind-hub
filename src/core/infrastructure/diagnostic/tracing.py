"""可选 OpenTelemetry Trace：Provider 由 Core 句柄独占，未启用时不导入 SDK。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import grpc


def create_tracer_provider(service_name: str, *, endpoint: str, sample_ratio: float) -> Any:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:
        raise RuntimeError("OTLP tracing requires opentelemetry-sdk and OTLP exporter") from exc
    del trace
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
        sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    return provider


class TraceServerInterceptor(grpc.aio.ServerInterceptor):
    """按外部 traceparent 提取父 Span，四种 grpc.aio 流形态共享同一语义。"""

    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        handler = await continuation(details)
        if handler is None:
            return None
        from opentelemetry import propagate
        from opentelemetry.trace import SpanKind, Status, StatusCode

        parent = propagate.extract(
            {str(k).lower(): str(v) for k, v in (details.invocation_metadata or ())}
        )
        tracer = self._tracer
        operation = str(details.method)

        def wrap(func: Any, response_streaming: bool) -> Any:
            if response_streaming:

                async def stream(request: Any, context: Any) -> Any:
                    with tracer.start_as_current_span(
                        operation, context=parent, kind=SpanKind.SERVER
                    ) as span:
                        try:
                            async for value in func(request, context):
                                yield value
                        except BaseException as exc:
                            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                            raise

                return stream

            async def unary(request: Any, context: Any) -> Any:
                with tracer.start_as_current_span(
                    operation, context=parent, kind=SpanKind.SERVER
                ) as span:
                    try:
                        return await func(request, context)
                    except BaseException as exc:
                        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                        raise

            return unary

        if handler.request_streaming:
            if handler.response_streaming:
                factory = grpc.stream_stream_rpc_method_handler
                wrapped = wrap(handler.stream_stream, True)
            else:
                factory = grpc.stream_unary_rpc_method_handler
                wrapped = wrap(handler.stream_unary, False)
        elif handler.response_streaming:
            factory = grpc.unary_stream_rpc_method_handler
            wrapped = wrap(handler.unary_stream, True)
        else:
            factory = grpc.unary_unary_rpc_method_handler
            wrapped = wrap(handler.unary_unary, False)
        return factory(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
