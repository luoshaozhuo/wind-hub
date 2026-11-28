"""FU-02 生产日志契约：生命周期身份、结算超时 WARNING、消费异常 ERROR、无敏感信息。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sink_service.application.latency_window import LatencyWindow
from sink_service.application.realtime_delivery import RealtimeDeliveryPort
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.reliable_runtime import ReliableDeliveryRuntime

pytestmark = [pytest.mark.unit]


class _Engine:
    def __init__(self) -> None:
        self.received_total = 0
        self.dropped_total = 0
        self.delivered_total = 0
        self.failed_total = 0
        self.write_success_total = 0
        self.latency_window = LatencyWindow(window_ms=60000)

    async def stop(self) -> None:
        return None


class _Ingress:
    async def remove(self, _delivery_id: str) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.releases = 0

    async def release(self, _sink_id: str) -> None:
        self.releases += 1


class _Writer:
    def __init__(self, *, settle_blocks: bool = False) -> None:
        self._settle_blocks = settle_blocks
        self.settled = asyncio.Event()
        if not settle_blocks:
            self.settled.set()

    async def settle(self) -> None:
        if self._settle_blocks:
            await self.settled.wait()


def _realtime_port(*, settle_blocks: bool = False) -> RealtimeDeliveryPort:
    port = object.__new__(RealtimeDeliveryPort)
    port._id = "d-log"
    port._sink_id = "sink-log"
    port._engine = _Engine()
    port._ingress = _Ingress()
    port._pool = _Pool()
    port._writer = _Writer(settle_blocks=settle_blocks)
    port._stopping = False
    port._settle_timeout = 0.01
    port._retained = {}
    port._retained_latency = LatencyWindow(window_ms=60000)
    return port


def test_realtime_stop_logs_identity_without_secrets(caplog: pytest.LogCaptureFixture) -> None:
    """正常停止输出 INFO，包含 delivery/sink 身份，不含令牌类内容。"""
    port = _realtime_port()
    with caplog.at_level("INFO", logger="sink_service.application.realtime_delivery"):
        asyncio.run(port.stop())
    text = caplog.text
    assert "Realtime Delivery stopped delivery=d-log sink=sink-log" in text
    assert "token" not in text.lower()
    assert "password" not in text.lower()


def test_realtime_settlement_timeout_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """settle 超时必须留下 WARNING，且异常语义不变（保留所有权）。"""
    port = _realtime_port(settle_blocks=True)
    with (
        caplog.at_level("WARNING", logger="sink_service.application.realtime_delivery"),
        pytest.raises(ConfigurationError, match="settlement timed out"),
    ):
        asyncio.run(port.stop())
    text = caplog.text
    assert "Realtime Delivery settlement timed out delivery=d-log sink=sink-log" in text
    assert port._stopping
    assert port._pool.releases == 0


def _reliable(*, stop_set: bool = False) -> ReliableDeliveryRuntime:
    runtime = object.__new__(ReliableDeliveryRuntime)
    runtime._stop = asyncio.Event()
    if stop_set:
        runtime._stop.set()
    runtime._task = None
    runtime._client = None
    runtime._subscription = None
    runtime._adapter = None
    runtime._definition = SimpleNamespace(delivery_id="h-1", sink_id="kafka-sink")
    runtime._snapshot = SimpleNamespace(
        revision="rev-42",
        service={"reliable": {"replay_starvation_alarm_seconds": 300}},
    )
    runtime._capacity_alarm = ""
    runtime._backlog = None
    runtime._last_backlog_progress = 0.0
    runtime._last_error = ""
    return runtime


def test_reliable_stop_logs_identity_revision(caplog: pytest.LogCaptureFixture) -> None:
    """正常停止输出 INFO（delivery/sink/revision），无敏感字段。"""
    runtime = _reliable()
    with caplog.at_level("INFO", logger="sink_service.infrastructure.reliable_runtime"):
        asyncio.run(runtime.stop())
    text = caplog.text
    assert "Reliable Delivery stopped delivery=h-1 sink=kafka-sink revision=rev-42" in text
    assert "token" not in text.lower()


def test_reliable_unexpected_exit_logs_error_without_exception_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """消费任务异常退出：ERROR 含身份；异常详情进 last_error 而非日志（不泄敏感栈）。"""
    runtime = _reliable()

    async def boom() -> None:
        raise RuntimeError("secret-internal-detail-xyz")

    async def scenario() -> None:
        task = asyncio.create_task(boom())
        await asyncio.gather(task, return_exceptions=True)
        runtime._on_task_finished(task)

    with caplog.at_level("ERROR", logger="sink_service.infrastructure.reliable_runtime"):
        asyncio.run(scenario())
    text = caplog.text
    assert "Reliable Delivery task terminated unexpectedly delivery=h-1 sink=kafka-sink" in text
    assert "secret-internal-detail-xyz" not in text
    assert "secret-internal-detail-xyz" in runtime.last_error


def test_reliable_normal_stop_task_exit_is_not_error(caplog: pytest.LogCaptureFixture) -> None:
    """stop 期间的任务收尾不得误报 ERROR。"""
    runtime = _reliable(stop_set=True)

    async def scenario() -> None:
        task = asyncio.create_task(asyncio.sleep(0))
        await asyncio.gather(task, return_exceptions=True)
        runtime._on_task_finished(task)

    with caplog.at_level("ERROR", logger="sink_service.infrastructure.reliable_runtime"):
        asyncio.run(scenario())
    assert "terminated unexpectedly" not in caplog.text


def test_reliable_cancelled_without_stop_logs_error(caplog: pytest.LogCaptureFixture) -> None:
    """非 STOP 语境下的任务取消视为异常退出（ERROR）。"""
    runtime = _reliable()

    async def scenario() -> None:
        task = asyncio.create_task(asyncio.sleep(60))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        runtime._on_task_finished(task)

    with caplog.at_level("ERROR", logger="sink_service.infrastructure.reliable_runtime"):
        asyncio.run(scenario())
    assert "terminated unexpectedly delivery=h-1 sink=kafka-sink" in caplog.text
