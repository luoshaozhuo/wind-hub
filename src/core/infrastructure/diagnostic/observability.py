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

from core.application.settings import LogSettings
from core.infrastructure.diagnostic.async_log import BoundedAsyncHandler
from core.infrastructure.diagnostic.log_setup import _DailySizeFileHandler
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
class LogFileSettings:
    enabled: bool = False
    path: str | None = None
    max_size_mb: int = 100
    retention_days: int = 7


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str = "INFO"
    format: str = "jsonl"
    console: bool = True
    file: LogFileSettings = field(default_factory=LogFileSettings)
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
        if log.format not in {"jsonl", "text"} or (
            type(log.queue_capacity) is not int or log.queue_capacity <= 0
        ):
            raise ValueError("invalid logging format or queue capacity")
        file = log.file
        if file.enabled and not file.path:
            raise ValueError("logging file requires an explicit path")
        if type(file.max_size_mb) is not int or file.max_size_mb <= 0:
            raise ValueError("logging.file.max_size_mb must be a positive integer")
        if type(file.retention_days) is not int or file.retention_days <= 0:
            raise ValueError("logging.file.retention_days must be a positive integer")
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
    """JSONL 公共字段由生产线程的观测 Filter 提供，不信任任意额外字段。"""

    def format(self, record: logging.LogRecord) -> str:
        values: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat().replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "service": getattr(record, "service", None),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": (
                None if getattr(record, "request_id", None) in (None, "", "-")
                else getattr(record, "request_id", None)
            ),
            "trace_id": getattr(record, "trace_id", None),
            "span_id": getattr(record, "span_id", None),
            "exception": self.formatException(record.exc_info) if record.exc_info else None,
            "attributes": {},
        }
        return json.dumps(values, ensure_ascii=False, allow_nan=False)


class _ContextLogFilter(logging.Filter):
    """在入队前冻结 Service/Trace 信息，防止线程切换导致上下文丢失。"""

    def __init__(self, service: str, *, tracing: bool) -> None:
        super().__init__()
        self._service = service
        self._tracing = tracing

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self._service
        record.trace_id = None
        record.span_id = None
        if self._tracing:
            try:
                from opentelemetry import trace

                context = trace.get_current_span().get_span_context()
                if context.is_valid:
                    record.trace_id = format(context.trace_id, "032x")
                    record.span_id = format(context.span_id, "016x")
            except Exception:
                # An observability backend must not block normal application logging.
                pass
        return True


class _Recorder:
    """实例级 Registry；公开指标仅允许低基数固定维度。"""

    def __init__(self, service_name: str, settings: MetricsSettings) -> None:
        from prometheus_client import CollectorRegistry, Counter, Histogram

        self.registry = CollectorRegistry(auto_describe=True)
        self.rpc_calls = Counter(
            "windhub_rpc_requests_total",
            "Completed RPC operations",
            ("service", "method", "status"),
            registry=self.registry,
        )
        self.rpc_duration = Histogram(
            "windhub_rpc_duration_seconds",
            "Completed RPC operation latency",
            ("service", "method", "status"),
            buckets=settings.buckets_seconds,
            registry=self.registry,
        )
        self.protocol_calls = Counter(
            "windhub_protocol_operations_total",
            "Completed industrial protocol operations",
            ("protocol", "operation", "status"),
            registry=self.registry,
        )
        self.protocol_duration = Histogram(
            "windhub_protocol_duration_seconds",
            "Industrial protocol operation latency",
            ("protocol", "operation", "status"),
            buckets=settings.buckets_seconds,
            registry=self.registry,
        )
        self.dropped = Counter(
            "windhub_observability_dropped_total",
            "Normal observability records dropped before export",
            ("channel", "reason"),
            registry=self.registry,
        )
        self.service_name = service_name

    def record(self, transport: str, operation: str, outcome: str, seconds: float) -> None:
        try:
            method = f"{transport}:{operation}" if transport != "grpc" else operation
            self.rpc_calls.labels(self.service_name, method, outcome).inc()
            self.rpc_duration.labels(
                self.service_name, method, outcome
            ).observe(seconds)
        except Exception:
            # Do not recursively log telemetry failures or mutate business outcomes.
            pass

    def record_protocol(
        self, protocol: str, operation: str, outcome: str, seconds: float
    ) -> None:
        try:
            self.protocol_calls.labels(protocol, operation, outcome).inc()
            self.protocol_duration.labels(
                protocol, operation, outcome
            ).observe(seconds)
        except Exception:
            # Optional metrics must not affect device I/O.
            pass

    def record_drop(self, channel: str, reason: str) -> None:
        try:
            self.dropped.labels(channel, reason).inc()
        except Exception:
            pass


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
                    headers_out = [
                        (key, value)
                        for key, value in message.get("headers", ())
                        if key.lower() != REQUEST_ID_HEADER.encode("ascii")
                    ]
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


class MetricsServerInterceptor(grpc.aio.ServerInterceptor):
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

        def complete_outcome(context: Any, raised: bool) -> str:
            if raised:
                return "failure"
            try:
                status = context.code()
            except Exception:
                # Observability failure must never replace an application result.
                return "unknown"
            if status is None or status is grpc.StatusCode.OK:
                return "success"
            return "failure"

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
                        recorder.record(
                            "grpc", operation,
                            complete_outcome(context, outcome == "failure"),
                            time.monotonic() - started,
                        )
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
                    recorder.record(
                        "grpc",
                        operation,
                        complete_outcome(context, outcome == "failure"),
                        time.monotonic() - started,
                    )
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
        self._leases = 1
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
        self._handler: BoundedAsyncHandler | None = None
        targets: list[logging.Handler] = []
        formatter: logging.Formatter = (
            JsonFormatter()
            if settings.logging.format == "jsonl"
            else logging.Formatter("%(levelname)s [%(request_id)s] %(message)s")
        )
        if settings.logging.console:
            targets.append(logging.StreamHandler())
        file_config = settings.logging.file
        if file_config.enabled:
            targets.append(
                _DailySizeFileHandler(
                    LogSettings(
                        level=settings.logging.level,
                        path=file_config.path or "",
                        console=False,
                        max_size_mb=float(file_config.max_size_mb),
                        retention_days=file_config.retention_days,
                    ),
                    service_name,
                )
            )
        if targets:
            for target in targets:
                target.setLevel(logging._nameToLevel[settings.logging.level])
                # RequestId 在生产线程入队前绑定，消费线程不能再次读取 contextvar。
                target.setFormatter(formatter)
            handler = BoundedAsyncHandler(
                targets, capacity=settings.logging.queue_capacity,
                process_name=service_name,
                on_drop=(
                    (lambda recorder=self._recorder: recorder.record_drop("logging", "queue_full"))
                    if self._recorder is not None else None
                ),
            )
            handler.setLevel(logging._nameToLevel[settings.logging.level])
            handler.addFilter(RequestIdFilter())
            handler.addFilter(
                _ContextLogFilter(service_name, tracing=settings.tracing.enabled)
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
            client_interceptors(
                correlation=settings.correlation.enabled,
                recorder=self._recorder,
                tracer=self._tracer,
            )
            if (
                settings.correlation.enabled
                or settings.metrics.enabled
                or settings.tracing.enabled
            )
            else ()
        )

    @property
    def registry(self) -> Any | None:
        return self._recorder.registry if self._recorder is not None else None

    def observe_protocol(self, driver: Any, *, protocol: str) -> Any:
        """在宿主装配阶段显式包装；指标关闭时保持原始端口。"""
        if self._closed:
            raise RuntimeError("observability is closed")
        if self._recorder is None:
            return driver
        from core.infrastructure.diagnostic.observed_protocol import ObservedProtocolPort

        return ObservedProtocolPort(
            driver, protocol=protocol, record=self._recorder.record_protocol
        )

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
        if self._leases > 1:
            self._leases -= 1
            return
        # A failed close remains retryable: retaining the logging worker/provider
        # is safer than claiming successful release while resources still exist.
        deadline = asyncio.get_running_loop().time() + timeout
        failures: list[str] = []
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            settled = await asyncio.to_thread(self._handler.stop, timeout=remaining)
            if not settled:
                failures.append("logging thread did not settle")
            else:
                self._handler = None
        if self._provider is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                failures.append("tracing shutdown deadline exhausted")
            else:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._provider.shutdown),
                        timeout=remaining,
                    )
                except TimeoutError:
                    failures.append("tracing provider shutdown timed out")
                else:
                    self._provider = None
        if failures:
            raise TimeoutError("; ".join(failures))
        self._closed = True



def parse_observability_settings(raw: Mapping[str, Any]) -> ObservabilitySettings:
    """严格解析宿主显式传入的 observability 子树，无固定路径或文件 I/O。"""
    if not isinstance(raw, Mapping):
        raise ValueError("observability must be a mapping")

    def section(name: str, allowed: frozenset[str]) -> dict[str, Any]:
        value = raw.get(name, {})
        if not isinstance(value, Mapping):
            raise ValueError(f"observability.{name} must be a mapping")
        unexpected = set(value) - allowed
        if unexpected:
            raise ValueError(f"unknown observability.{name} fields: {sorted(unexpected)}")
        return dict(value)

    unknown = set(raw) - {"logging", "correlation", "metrics", "tracing"}
    if unknown:
        raise ValueError(f"unknown observability sections: {sorted(unknown)}")
    log = section("logging", frozenset({
        "level", "format", "console", "file", "queue_capacity",
    }))
    file_raw = log.pop("file", {})
    if not isinstance(file_raw, Mapping):
        raise ValueError("observability.logging.file must be a mapping")
    unknown_file = set(file_raw) - {
        "enabled", "path", "max_size_mb", "retention_days",
    }
    if unknown_file:
        raise ValueError(f"unknown observability.logging.file fields: {sorted(unknown_file)}")
    correlation = section("correlation", frozenset({"enabled"}))
    metrics = section("metrics", frozenset({"enabled", "buckets_seconds"}))
    tracing = section("tracing", frozenset({
        "enabled", "sample_ratio", "otlp_endpoint",
    }))
    bool_fields = (
        (log, "console"),
        (file_raw, "enabled"),
        (correlation, "enabled"),
        (metrics, "enabled"),
        (tracing, "enabled"),
    )
    if any(key in mapping and type(mapping[key]) is not bool for mapping, key in bool_fields):
        raise ValueError("observability enabled/console fields must be boolean")
    if "buckets_seconds" in metrics:
        value = metrics["buckets_seconds"]
        if type(value) is not list or any(type(x) not in (float, int) for x in value):
            raise ValueError("metrics.buckets_seconds must be an array of finite numbers")
        metrics["buckets_seconds"] = tuple(float(x) for x in value)
    if "sample_ratio" in tracing:
        if type(tracing["sample_ratio"]) not in (float, int):
            raise ValueError("tracing.sample_ratio must be numeric")
        tracing["sample_ratio"] = float(tracing["sample_ratio"])
    return ObservabilitySettings(
        logging=LoggingSettings(**log, file=LogFileSettings(**file_raw)),
        correlation=CorrelationSettings(**correlation),
        metrics=MetricsSettings(**metrics),
        tracing=TracingSettings(**tracing),
    )

_ACTIVE: dict[str, ObservabilityHandle] = {}


def configure_observability(
    settings: ObservabilitySettings, *, service_name: str
) -> ObservabilityHandle:
    """相同服务同配置复用句柄；配置冲突立即拒绝。"""
    if not service_name.strip() or not all(
        ch.isascii() and (ch.isalnum() or ch in "-_") for ch in service_name
    ):
        raise ValueError("service_name must not be empty")
    previous = _ACTIVE.get(service_name)
    if previous is not None and not previous._closed:
        if previous.settings != settings:
            raise ValueError("observability already configured with different settings")
        previous._leases += 1
        return previous
    handle = ObservabilityHandle(settings, service_name)
    _ACTIVE[service_name] = handle
    return handle
