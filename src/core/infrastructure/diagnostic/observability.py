"""非侵入式技术边界观测装配：实例级资源，不管理业务服务。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import grpc

from core.infrastructure.diagnostic.correlation import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    bind_request_id,
    get_request_id,
    new_request_id,
    normalize_request_id,
)
from core.infrastructure.rpc.correlation import RequestIdServerInterceptor
from core.infrastructure.rpc.client_correlation import client_interceptors
from core.infrastructure.diagnostic.tracing import TraceServerInterceptor, create_tracer_provider

_DEFAULT_BUCKETS = (0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0)


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str = "INFO"
    format: str = "jsonl"
    console: bool = True
    queue_capacity: int = 10000


@dataclass(frozen=True, slots=True)
class CorrelationSettings:
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class MetricsSettings:
    enabled: bool = False
    buckets_seconds: tuple[float, ...] = _DEFAULT_BUCKETS


@dataclass(frozen=True, slots=True)
class TracingSettings:
    enabled: bool = False
    sample_ratio: float = 0.1
    otlp_endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    correlation: CorrelationSettings = field(default_factory=CorrelationSettings)
    metrics: MetricsSettings = field(default_factory=MetricsSettings)
    tracing: TracingSettings = field(default_factory=TracingSettings)

    def __post_init__(self) -> None:
        log = self.logging
        if log.level not in logging._nameToLevel or not isinstance(
            logging._nameToLevel[log.level], int
        ):
            raise ValueError("unknown logging level")
        if log.format not in {"jsonl", "text"} or log.queue_capacity <= 0:
            raise ValueError("invalid logging format or queue capacity")
        buckets = self.metrics.buckets_seconds
        if not buckets or any(
            not math.isfinite(value) or value <= 0 for value in buckets
        ) or any(left >= right for left, right in zip(buckets, buckets[1:])):
            raise ValueError("metric buckets must be positive and strictly increasing")
        tracing = self.tracing
        if not math.isfinite(tracing.sample_ratio) or not 0 <= tracing.sample_ratio <= 1:
            raise ValueError("trace sample_ratio must be within 0..1")
        if tracing.enabled and not tracing.otlp_endpoint:
            raise ValueError("tracing requires an explicit otlp_endpoint")


class JsonFormatter(logging.Formatter):
    """基础 JSONL，控制字段不接受请求级任意字典覆盖。"""

    def format(self, record: logging.LogRecord) -> str:
        values: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            values["exception"] = self.formatException(record.exc_info)
        return json.dumps(values, ensure_ascii=False, allow_nan=False)


class _Recorder:
    """Bounded-label Prometheus registry owned by one service handle."""

    def __init__(self, service_name: str, settings: MetricsSettings):
        from prometheus_client import CollectorRegistry, Counter, Histogram

        self.registry = CollectorRegistry(auto_describe=True)
        self.calls = Counter(
            "wind_hub_boundary_calls_total",
            "Boundary requests by transport, operation and coarse outcome",
            ("service", "transport", "operation", "outcome"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "wind_hub_boundary_duration_seconds",
            "Time spent inside a technical boundary",
            ("service", "transport", "operation"),
            buckets=settings.buckets_seconds,
            registry=self.registry,
        )
        self.service_name = service_name

    def record(self, transport: str, operation: str, outcome: str, seconds: float) -> None:
        try:
            self.calls.labels(self.service_name, transport, operation, outcome).inc()
            self.duration.labels(self.service_name, transport, operation).observe(seconds)
        except Exception:
            logging.getLogger(__name__).exception("Metrics export failed")


class RequestIdASGIMiddleware:
    """纯 ASGI 中间件，不改动 FastAPI 业务签名。"""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        correlation: bool = True,
        recorder: _Recorder | None = None,
        tracer: Any | None = None,
    ) -> None:
        self.app = app
        self.correlation = correlation
        self.recorder = recorder
        self.tracer = tracer

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", ())
        request_id = ""
        for raw_key, raw_value in headers:
            if raw_key.lower() == REQUEST_ID_HEADER.encode("ascii"):
                request_id = normalize_request_id(raw_value.decode("utf-8", "replace"))
                break
        request_id = request_id or new_request_id()
        method = str(scope.get("method", "UNKNOWN"))
        # Do not use URL paths as labels (unbounded path parameters).
        started = time.monotonic()
        status = 500

        async def send_with_id(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
                if self.correlation:
                    headers_out = list(message.get("headers", ()))
                    headers_out.append(
                        (REQUEST_ID_HEADER.encode("ascii"), request_id.encode("ascii"))
                    )
                    message = {**message, "headers": headers_out}
            await send(message)

        try:
            if self.tracer is not None:
                from opentelemetry import propagate
                from opentelemetry.trace import SpanKind
                carrier = {
                    k.decode("ascii", "replace"): v.decode("ascii", "replace")
                    for k, v in headers
                }
                parent = propagate.extract(carrier)
                with self.tracer.start_as_current_span(
                    f"HTTP {method}", context=parent, kind=SpanKind.SERVER
                ):
                    await self._invoke(request_id, scope, receive, send_with_id)
            else:
                await self._invoke(request_id, scope, receive, send_with_id)
        finally:
            if self.recorder is not None:
                self.recorder.record(
                    "http", method,
                    "success" if status < 500 else "failure",
                    time.monotonic() - started,
                )

    async def _invoke(
        self,
        request_id: str,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if self.correlation:
            with bind_request_id(request_id):
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


class MetricsServerInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    """覆盖四种 gRPC 流模式，统计只包含固定方法与成功/失败结果。"""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        handler = await continuation(details)
        if handler is None:
            return None
        # Method identifiers come from server registrations, not caller-supplied metadata.
        operation = str(details.method)
        recorder = self._recorder

        def observe(behavior: Callable[..., Any], stream: bool) -> Callable[..., Any]:
            if stream:
                async def streaming(req: Any, context: Any) -> Any:
                    started = time.monotonic()
                    outcome = "success"
                    try:
                        async for response in behavior(req, context):
                            yield response
                    except BaseException:
                        outcome = "failure"
                        raise
                    finally:
                        recorder.record("grpc", operation, outcome, time.monotonic() - started)
                return streaming

            async def unary(req: Any, context: Any) -> Any:
                started = time.monotonic()
                outcome = "success"
                try:
                    return await behavior(req, context)
                except BaseException:
                    outcome = "failure"
                    raise
                finally:
                    recorder.record("grpc", operation, outcome, time.monotonic() - started)
            return unary

        if handler.request_streaming:
            if handler.response_streaming:
                factory = grpc.stream_stream_rpc_method_handler
                wrapped = observe(handler.stream_stream, True)
            else:
                factory = grpc.stream_unary_rpc_method_handler
                wrapped = observe(handler.stream_unary, False)
        elif handler.response_streaming:
            factory = grpc.unary_stream_rpc_method_handler
            wrapped = observe(handler.unary_stream, True)
        else:
            factory = grpc.unary_unary_rpc_method_handler
            wrapped = observe(handler.unary_unary, False)
        return factory(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


class ObservabilityHandle:
    """一个服务独占注册表及日志 Handler；关闭不影响其他宿主。"""

    def __init__(self, settings: ObservabilitySettings, service_name: str) -> None:
        self.settings = settings
        self.service_name = service_name
        self._recorder = (
            _Recorder(service_name, settings.metrics) if settings.metrics.enabled else None
        )
        self._closed = False
        self._provider: Any | None = None
        self._tracer: Any | None = None
        if settings.tracing.enabled:
            self._provider = create_tracer_provider(
                service_name,
                endpoint=settings.tracing.otlp_endpoint or "",
                sample_ratio=settings.tracing.sample_ratio,
            )
            self._tracer = self._provider.get_tracer("wind-hub-core")
        self._apps: set[int] = set()
        self._logger = logging.getLogger()
        self._handler: logging.Handler | None = None
        if settings.logging.console:
            handler = logging.StreamHandler()
            handler.setLevel(logging._nameToLevel[settings.logging.level])
            handler.addFilter(RequestIdFilter())
            handler.setFormatter(
                JsonFormatter()
                if settings.logging.format == "jsonl"
                else logging.Formatter("%(levelname)s [%(request_id)s] %(message)s")
            )
            self._logger.addHandler(handler)
            self._handler = handler
        interceptors: list[grpc.aio.ServerInterceptor] = []
        if settings.correlation.enabled:
            interceptors.append(RequestIdServerInterceptor())
        if self._recorder is not None:
            interceptors.append(MetricsServerInterceptor(self._recorder))
        if self._tracer is not None:
            interceptors.append(TraceServerInterceptor(self._tracer))
        self.grpc_server_interceptors = tuple(interceptors)
        self.grpc_client_interceptors = (
            client_interceptors(tracing=settings.tracing.enabled)
            if settings.correlation.enabled or settings.tracing.enabled
            else ()
        )

    @property
    def registry(self) -> Any | None:
        return self._recorder.registry if self._recorder is not None else None

    def instrument_asgi(self, app: Any) -> None:
        if self._closed:
            raise RuntimeError("observability is closed")
        identity = id(app)
        if identity in self._apps:
            return
        app.add_middleware(
            RequestIdASGIMiddleware,
            correlation=self.settings.correlation.enabled,
            recorder=self._recorder,
            tracer=self._tracer,
        )
        self._apps.add(identity)

    async def close(self, *, timeout: float = 5.0) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if self._closed:
            return
        self._closed = True
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
            self._handler.flush()
            self._handler.close()
            self._handler = None
        if self._provider is not None:
            await asyncio.wait_for(asyncio.to_thread(self._provider.shutdown), timeout=timeout)
            self._provider = None


_ACTIVE: dict[str, ObservabilityHandle] = {}


def configure_observability(
    settings: ObservabilitySettings, *, service_name: str
) -> ObservabilityHandle:
    """相同服务同配置复用句柄；配置冲突立即拒绝。"""
    if not service_name.strip():
        raise ValueError("service_name must not be empty")
    previous = _ACTIVE.get(service_name)
    if previous is not None and not previous._closed:
        if previous.settings != settings:
            raise ValueError("observability already configured with different settings")
        return previous
    handle = ObservabilityHandle(settings, service_name)
    _ACTIVE[service_name] = handle
    return handle
