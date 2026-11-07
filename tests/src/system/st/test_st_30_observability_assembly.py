"""ST-30 观测装配：仅改装配代码即可启用观测，业务行为零变化（设计 §14.3 ST-30）。

覆盖：
1. 开关一致性：metrics 关闭时 observe_protocol 返回原端口；开启后包装，
   同一条业务调用的返回值与异常在两种装配下完全一致。
2. 装配一致性：gRPC 拦截器随 correlation/metrics 开关增减；registry 仅在
   metrics 开启时存在；tracing 缺 endpoint 在设置校验期被拒绝。
3. JSON Lines：JsonFormatter 输出单行可解析 JSON，公共字段齐全。
4. 观测出口故障：record 回调抛异常不得改变业务结果或吞掉业务异常。
5. 日志背压：BoundedAsyncHandler 队列满后丢弃并计数，emit 永不阻塞，
   on_drop 自身故障被隔离。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from core.application.protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
)
from core.infrastructure.crosscutting.async_log import BoundedAsyncHandler
from core.infrastructure.crosscutting.observability import (
    CorrelationSettings,
    JsonFormatter,
    LoggingSettings,
    MetricsSettings,
    ObservabilityHandle,
    ObservabilitySettings,
    TracingSettings,
)
from core.infrastructure.crosscutting.observed_protocol import ObservedProtocolPort

pytestmark = [pytest.mark.system, pytest.mark.reliability, pytest.mark.overload]

_QUIET_LOGGING = LoggingSettings(console=False)


class _FakeDriver:
    """最小可读协议端口；记录调用以证明包装层不改变调用形状。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def connect(self) -> None:
        self.calls.append("connect")

    async def health(self) -> ConnectionHealth:
        self.calls.append("health")
        return ConnectionHealth(True, "ok")

    async def read_one(self, point_id: str) -> ProtocolSample:
        self.calls.append(f"read:{point_id}")
        return ProtocolSample(point_id=point_id, value=42, timestamp=datetime.now(UTC))

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset({ProtocolCapability.READ})


def _settings(*, metrics: bool, correlation: bool = False) -> ObservabilitySettings:
    return ObservabilitySettings(
        logging=_QUIET_LOGGING,
        correlation=CorrelationSettings(enabled=correlation),
        metrics=MetricsSettings(enabled=metrics),
    )


async def test_st_30_toggle_changes_wrapper_not_business_behavior() -> None:
    driver_off = _FakeDriver()
    handle_off = ObservabilityHandle(_settings(metrics=False), "st30-off")
    assert handle_off.observe_protocol(driver_off, protocol="modbus") is driver_off
    assert handle_off.registry is None
    assert handle_off.grpc_server_interceptors == ()

    driver_on = _FakeDriver()
    handle_on = ObservabilityHandle(_settings(metrics=True, correlation=True), "st30-on")
    wrapped = handle_on.observe_protocol(driver_on, protocol="modbus")
    assert isinstance(wrapped, ObservedProtocolPort)
    assert wrapped is not driver_on
    assert handle_on.registry is not None
    # correlation + metrics 两个拦截器，顺序与装配声明一致。
    assert len(handle_on.grpc_server_interceptors) == 2

    # 同一业务调用在两种装配下结果逐字段一致，且都真实到达 Driver。
    await handle_off.observe_protocol(driver_off, protocol="modbus").connect()
    await wrapped.connect()
    sample_a = await handle_off.observe_protocol(driver_off, protocol="modbus").read_one("p1")
    sample_b = await wrapped.read_one("p1")
    assert sample_a.point_id == sample_b.point_id == "p1"
    assert sample_a.value == sample_b.value == 42
    assert driver_off.calls == driver_on.calls

    await handle_off.close()
    await handle_on.close()


async def test_st_30_exporter_failure_does_not_mutate_protocol_outcome() -> None:
    handle = ObservabilityHandle(_settings(metrics=True), "st30-export-fail")
    driver = _FakeDriver()
    wrapped = handle.observe_protocol(driver, protocol="modbus")

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("observability backend down")

    # 观测出口（指标记录）故障必须被隔离，业务调用照常成功。
    handle._recorder.record_protocol = explode  # type: ignore[method-assign]
    await wrapped.connect()
    sample = await wrapped.read_one("p2")
    assert sample.point_id == "p2" and sample.value == 42

    async def boom() -> None:
        raise ValueError("business failure")

    driver.connect = boom  # type: ignore[method-assign]
    # 业务异常必须原样透传，不能被观测层替换或吞掉。
    with pytest.raises(ValueError, match="business failure"):
        await wrapped.connect()
    await handle.close()


def test_st_30_jsonl_formatter_emits_single_parseable_line() -> None:
    record = logging.LogRecord(
        name="st30",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="structured %s",
        args=("event",),
        exc_info=None,
    )
    record.request_id = "req-1"
    line = JsonFormatter().format(record)
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["message"] == "structured event"
    assert payload["request_id"] == "req-1"
    assert payload["trace_id"] is None and payload["exception"] is None
    assert set(payload) >= {
        "timestamp",
        "level",
        "service",
        "logger",
        "message",
        "request_id",
        "trace_id",
        "span_id",
        "exception",
        "attributes",
    }


def test_st_30_bounded_log_handler_drops_under_backpressure_without_blocking() -> None:
    gate = threading.Event()

    class BlockingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            gate.wait(timeout=5)

    drops: list[int] = []

    def on_drop() -> None:
        drops.append(1)

    handler = BoundedAsyncHandler(
        (BlockingHandler(),), capacity=1, process_name="st30", on_drop=on_drop
    )
    record = logging.LogRecord("st30", logging.INFO, __file__, 1, "x", (), None)
    handler.emit(record)  # 占住唯一队列位
    # 后续入队立即失败计数，不阻塞业务线程。
    for _ in range(5):
        handler.emit(record)
    assert handler.dropped == 5
    assert len(drops) == 5

    # on_drop 自身抛异常也不得影响 emit 的隔离性。
    def bad_drop() -> None:
        raise RuntimeError("drop callback broken")

    handler._on_drop = bad_drop
    handler.emit(record)
    assert handler.dropped == 6

    gate.set()
    assert handler.stop(timeout=5) is True


def test_st_30_tracing_requires_explicit_endpoint_at_settings_validation() -> None:
    # OTLP 接线必须显式声明 endpoint，禁止静默指向硬编码默认值。
    with pytest.raises(ValueError, match="otlp_endpoint"):
        ObservabilitySettings(
            logging=_QUIET_LOGGING,
            tracing=TracingSettings(enabled=True, otlp_endpoint=None),
        )
    # 合法装配（不导出 span，故不依赖网络）；OTLP 真实链路在无采集器环境记 NOT_RUN。
    ok = ObservabilitySettings(
        logging=_QUIET_LOGGING,
        tracing=TracingSettings(enabled=True, otlp_endpoint="http://127.0.0.1:4317"),
    )
    assert ok.tracing.enabled is True


async def test_st_30_close_is_idempotent_and_bounded() -> None:
    handle = ObservabilityHandle(_settings(metrics=True), "st30-close")
    await handle.close(timeout=2.0)
    # 重复关闭直接返回，不抛异常、不重复释放。
    await handle.close(timeout=2.0)
    with pytest.raises(RuntimeError, match="closed"):
        handle.observe_protocol(_FakeDriver(), protocol="modbus")
