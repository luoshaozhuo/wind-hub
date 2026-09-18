"""Runtime（``application/runtime``）的单元测试。

验证对象：:class:`Runtime`——组件生命周期与状态编排核心。

覆盖点：

- ``start`` / ``stop``：设备连接（best-effort）、点表注入、sink 打开与
  消费者任务、调度器启动与轮询 Job 注册、优雅停机、幂等；
- ``running`` 语义（决策 1）与 ``health``（设备优先、打开失败的 sink 暴露
  为不健康）；
- 设备热管理：``add_device`` / ``remove_device`` / ``rebuild_device``；
- sink 热管理：``add_sink`` / ``remove_sink`` / ``rebuild_sink``（队列保留）；
- ``replace_router`` / ``replace_pipeline`` 委托引擎；
- ``reconfigure``：按 diff 编排、路由/管线重建、阶段错误隔离；
- 订阅模式与回退轮询；
- 背压策略与路由/丢弃计数（决策 7）。

调度器使用内存 Fake（实现 ``SchedulerPort``），不依赖真实 APScheduler——
适配器契约在 ``tests/unit/scheduling/test_scheduler.py`` 单独验证。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.application.runtime import Runtime
from wind_hub.config.schema import (
    Config,
    DeviceConfig,
    DevicesConfig,
    PipelineConfig,
    PointAddress,
    PointConfig,
    PointsConfig,
    PollingGroup,
    RoutingConfig,
    SchedulerConfig,
    SinkConfig,
    SystemConfig,
)
from wind_hub.domain.acquisition import AcquisitionEngine
from wind_hub.domain.command import Dispatcher
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ProtocolError
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.model.reload import ConfigDiff, DeviceDiff, SinkDiff
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort, SinkPort
from wind_hub.domain.port.scheduling import JobInfo
from wind_hub.domain.processing import Pipeline
from wind_hub.domain.routing import Router

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_device(
    device_id: str,
    polling_interval: float = 1.0,
    mode: str = "poll",
) -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol="modbus",
        endpoint=Endpoint(host="10.0.0.1", port=502),
        polling=[PollingGroup(group="default", interval=polling_interval)],
        enabled=True,
        mode=mode,
    )


def _make_config(backpressure: str = "drop_old") -> SchedulerConfig:
    return SchedulerConfig(
        queue_maxsize=10,
        backpressure_policy=backpressure,
        shutdown_timeout=1.0,
        connect_timeout=1.0,
        read_timeout=1.0,
    )


def _make_mock_protocol() -> ProtocolPort:
    proto = MagicMock(spec=ProtocolPort)
    proto.set_points_mapping = MagicMock()
    proto.connect = AsyncMock()
    proto.close = AsyncMock()
    proto.read = AsyncMock(return_value=[])
    proto.write = AsyncMock()
    proto.subscribe = AsyncMock()
    proto.health = MagicMock(return_value=HealthStatus(healthy=True))
    return proto


def _make_point(device_id: str, point_id: str) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        device_id=device_id,
        address=PointAddress(type="holding_register"),
    )


def _make_mock_sink() -> SinkPort:
    sink = MagicMock(spec=SinkPort)
    sink.open = AsyncMock()
    sink.close = AsyncMock()
    sink.write = AsyncMock()
    sink.flush = AsyncMock()
    sink.health = MagicMock(return_value=HealthStatus(healthy=True))
    return sink


def _value(device_id: str = "d1", point_id: str = "p1") -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=1.0)


class _FakeScheduler:
    """``SchedulerPort`` 的内存实现——记录 Job 注册，不执行任何调度。"""

    def __init__(self) -> None:
        self.jobs: dict[str, tuple[float, Callable[..., Awaitable[None]], tuple]] = {}
        self.started = False

    @property
    def running(self) -> bool:
        return self.started

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    def add_interval_job(
        self,
        job_id: str,
        interval_seconds: float,
        func: Callable[..., Awaitable[None]],
        args: tuple = (),
        replace_existing: bool = False,
    ) -> None:
        if job_id in self.jobs and not replace_existing:
            raise ValueError(f"duplicate job: {job_id}")
        self.jobs[job_id] = (interval_seconds, func, args)

    def remove_job(self, job_id: str) -> None:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        del self.jobs[job_id]

    def pause_job(self, job_id: str) -> None:
        if job_id not in self.jobs:
            raise KeyError(job_id)

    def resume_job(self, job_id: str) -> None:
        if job_id not in self.jobs:
            raise KeyError(job_id)

    async def trigger_job(self, job_id: str) -> None:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        _, func, args = self.jobs[job_id]
        await func(*args)

    def get_job(self, job_id: str) -> JobInfo | None:
        if job_id not in self.jobs:
            return None
        return JobInfo(job_id=job_id, next_run_time=datetime.now(UTC), paused=False)

    def list_jobs(self) -> list[JobInfo]:
        return [
            JobInfo(job_id=jid, next_run_time=datetime.now(UTC), paused=False)
            for jid in self.jobs
        ]


def _make_runtime(
    devices: dict[str, DeviceConfig] | None = None,
    protocols: dict[str, ProtocolPort] | None = None,
    sinks: dict[str, SinkPort] | None = None,
    config: SchedulerConfig | None = None,
    points_by_device: dict[str, list[PointConfig]] | None = None,
    protocol_factory=None,
    sink_factory=None,
    processor_factory=None,
) -> tuple[Runtime, AcquisitionEngine, _FakeScheduler]:
    """构造 Runtime + 真实引擎 + Fake 调度器，返回三元组。"""
    protocols = protocols if protocols is not None else {}
    points_by_device = points_by_device if points_by_device is not None else {}
    engine = AcquisitionEngine(
        protocols=protocols,
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        points_by_device=points_by_device,
    )
    scheduler = _FakeScheduler()
    runtime = Runtime(
        devices=devices if devices is not None else {},
        protocols=protocols,
        sinks=sinks if sinks is not None else {},
        engine=engine,
        scheduler=scheduler,
        dispatcher=MagicMock(spec=Dispatcher),
        config=config or _make_config(),
        points_by_device=points_by_device,
        protocol_factory=protocol_factory,
        sink_factory=sink_factory,
        processor_factory=processor_factory,
    )
    return runtime, engine, scheduler


def _make_full_config(
    devices: list[DeviceConfig] | None = None,
    sinks: list[SinkConfig] | None = None,
    points: list[PointConfig] | None = None,
) -> Config:
    return Config(
        system=SystemConfig(sinks=sinks or [], pipeline=PipelineConfig(processors=[])),
        devices=DevicesConfig(devices=devices or []),
        points=PointsConfig(points=points or []),
        routing=RoutingConfig(rules=[]),
    )


# ---------------------------------------------------------------------------
# start() —— 设备连接 / 点表注入 / Job 注册
# ---------------------------------------------------------------------------


async def test_start_injects_points_connects_and_registers_jobs() -> None:
    p1 = _make_mock_protocol()
    points = {"d1": [_make_point("d1", "p1")]}
    runtime, engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", polling_interval=2.0)},
        protocols={"d1": p1},
        points_by_device=points,
    )
    try:
        await runtime.start()

        p1.set_points_mapping.assert_called_once_with(points["d1"])
        p1.connect.assert_awaited_once()
        assert scheduler.started is True

        # 轮询 Job 以 poll:{device}:{group} 注册，执行体为引擎 collect
        interval, func, args = scheduler.jobs["poll:d1:default"]
        assert interval == 2.0
        assert func == engine.collect
        assert args == ("d1", "default")
        assert runtime.running is True
    finally:
        await runtime.stop()
    assert scheduler.started is False


async def test_start_is_idempotent() -> None:
    p1 = _make_mock_protocol()
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")}, protocols={"d1": p1}
    )
    await runtime.start()
    await runtime.start()
    p1.connect.assert_awaited_once()
    await runtime.stop()


async def test_connect_failure_skipped_others_continue() -> None:
    p1 = _make_mock_protocol()
    p2 = _make_mock_protocol()
    p1.connect.side_effect = OSError("refused")
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1"), "d2": _make_device("d2")},
        protocols={"d1": p1, "d2": p2},
    )
    try:
        await runtime.start()
    finally:
        await runtime.stop()

    p2.connect.assert_awaited_once()
    assert "poll:d1:default" in scheduler.jobs
    assert "poll:d2:default" in scheduler.jobs


async def test_connection_level_failure_logs_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """连接级故障（OSError/超时）是现场日常：简洁 warning，不打堆栈（决策 0.3）。"""
    p1 = _make_mock_protocol()
    p1.connect.side_effect = ConnectionRefusedError("refused")
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")}, protocols={"d1": p1}
    )
    with caplog.at_level(logging.WARNING, logger="wind_hub.application.runtime.runtime"):
        await runtime.start()
    await runtime.stop()

    conn_records = [r for r in caplog.records if "failed to connect" in r.message]
    assert len(conn_records) == 1
    assert conn_records[0].exc_info is None


async def test_wrapped_connection_failure_detected_through_cause_chain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """驱动把底层 OSError 包装进 ProtocolError 时沿 __cause__ 链穿透判定。"""
    p1 = _make_mock_protocol()
    wrapped = ProtocolError("connect failed")
    wrapped.__cause__ = OSError("tcp down")  # 模拟驱动 raise ... from 的包装链
    p1.connect.side_effect = wrapped
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")}, protocols={"d1": p1}
    )
    with caplog.at_level(logging.WARNING, logger="wind_hub.application.runtime.runtime"):
        await runtime.start()
    await runtime.stop()

    conn_records = [r for r in caplog.records if "failed to connect" in r.message]
    assert len(conn_records) == 1
    assert conn_records[0].exc_info is None


async def test_non_connection_failure_keeps_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """非连接级异常（实现缺陷）保留完整堆栈便于排查。"""
    p1 = _make_mock_protocol()
    p1.connect.side_effect = ValueError("bad config")
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")}, protocols={"d1": p1}
    )
    with caplog.at_level(logging.WARNING, logger="wind_hub.application.runtime.runtime"):
        await runtime.start()
    await runtime.stop()

    conn_records = [r for r in caplog.records if "failed to connect" in r.message]
    assert len(conn_records) == 1
    assert conn_records[0].exc_info is not None


async def test_disabled_device_registers_no_job() -> None:
    p1 = _make_mock_protocol()
    device = _make_device("d1")
    device.enabled = False
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": device}, protocols={"d1": p1}
    )
    try:
        await runtime.start()
        assert scheduler.jobs == {}
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# subscribe 模式与回退
# ---------------------------------------------------------------------------


async def test_subscribe_mode_device_is_not_polled() -> None:
    p1 = _make_mock_protocol()
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", mode="subscribe")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("d1", "p1")]},
    )
    try:
        await runtime.start()
        assert scheduler.jobs == {}
        p1.subscribe.assert_awaited_once()
    finally:
        await runtime.stop()


async def test_subscribe_not_supported_falls_back_to_polling() -> None:
    p1 = _make_mock_protocol()
    p1.subscribe.side_effect = NotImplementedError("no subscribe")
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", mode="subscribe")},
        protocols={"d1": p1},
    )
    try:
        await runtime.start()
        assert "poll:d1:default" in scheduler.jobs
    finally:
        await runtime.stop()


async def test_subscribe_failure_falls_back_to_polling() -> None:
    p1 = _make_mock_protocol()
    p1.subscribe.side_effect = OSError("subscribe broken")
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", mode="subscribe")},
        protocols={"d1": p1},
    )
    try:
        await runtime.start()
        assert "poll:d1:default" in scheduler.jobs
    finally:
        await runtime.stop()


async def test_subscribe_push_flows_through_engine() -> None:
    """订阅推送回调经 process_and_route 进入采集链路，计数口径一致。"""
    p1 = _make_mock_protocol()
    captured_callback = None

    async def _subscribe(refs, on_data):
        nonlocal captured_callback
        captured_callback = on_data

    p1.subscribe.side_effect = _subscribe
    runtime, engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1", mode="subscribe")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("d1", "p1")]},
    )
    try:
        await runtime.start()
        assert captured_callback is not None
        await captured_callback(_value())
        assert engine.points_collected == 1
        assert runtime.points_collected == 1
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# sink 打开与消费者
# ---------------------------------------------------------------------------


async def test_sink_open_failure_marked_unhealthy() -> None:
    bad = _make_mock_sink()
    bad.open.side_effect = OSError("cannot open")
    good = _make_mock_sink()
    runtime, _engine, _scheduler = _make_runtime(sinks={"bad": bad, "good": good})
    try:
        await runtime.start()
        good.open.assert_awaited_once()
        health = runtime.health()
        assert health["bad"].healthy is False
        assert health["good"].healthy is True
    finally:
        await runtime.stop()


async def test_health_lists_devices_first_then_sinks() -> None:
    p1 = _make_mock_protocol()
    s1 = _make_mock_sink()
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        sinks={"s1": s1},
    )
    health = runtime.health()
    assert list(health.keys()) == ["d1", "s1"]


async def test_dispatched_batch_reaches_sink_write() -> None:
    """完整链路：引擎 collect → 路由 → Runtime 派发 → 队列 → sink.write。"""
    p1 = _make_mock_protocol()
    p1.read = AsyncMock(return_value=[_value()])
    s1 = _make_mock_sink()
    write_done = asyncio.Event()

    async def _write(_batch):
        write_done.set()

    s1.write.side_effect = _write
    runtime, engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        sinks={"s1": s1},
        points_by_device={"d1": [_make_point("d1", "p1")]},
    )
    # 路由替身把全部点值导向 s1
    router = MagicMock(spec=Router)
    router.route.side_effect = lambda vals: {"s1": list(vals)}
    await engine.replace_router(router)

    try:
        await runtime.start()
        await engine.collect("d1", "default")
        # 消费者是独立任务：事件驱动等待而非固定 sleep
        await asyncio.wait_for(write_done.wait(), timeout=2.0)

        s1.write.assert_awaited_once()
        written = s1.write.await_args.args[0]
        assert len(written) == 1
        assert runtime.points_collected == 1
        assert runtime.points_routed == 1
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# stop() —— 优雅停机
# ---------------------------------------------------------------------------


async def test_stop_flushes_and_closes_sinks_and_protocols() -> None:
    p1 = _make_mock_protocol()
    s1 = _make_mock_sink()
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        sinks={"s1": s1},
    )
    await runtime.start()
    await runtime.stop()

    s1.flush.assert_awaited_once()
    s1.close.assert_awaited_once()
    p1.close.assert_awaited_once()
    assert runtime.running is False


async def test_stop_is_idempotent() -> None:
    runtime, _engine, _scheduler = _make_runtime()
    await runtime.stop()  # 未启动时直接返回
    await runtime.start()
    await runtime.stop()
    await runtime.stop()  # 重复停机无副作用


async def test_stop_tolerates_sink_flush_close_failures() -> None:
    s1 = _make_mock_sink()
    s1.flush.side_effect = OSError("flush boom")
    s1.close.side_effect = OSError("close boom")
    runtime, _engine, _scheduler = _make_runtime(sinks={"s1": s1})
    await runtime.start()
    await runtime.stop()  # 失败只记日志，停机链路走完
    assert runtime.running is False


# ---------------------------------------------------------------------------
# running 语义（决策 1）
# ---------------------------------------------------------------------------


async def test_running_false_before_start_and_after_stop() -> None:
    runtime, _engine, _scheduler = _make_runtime()
    assert runtime.running is False
    await runtime.start()
    assert runtime.running is True
    await runtime.stop()
    assert runtime.running is False


async def test_running_false_while_start_in_progress() -> None:
    """start 未完成（如设备 connect 仍在等待）时 running 保持 False。"""
    p1 = _make_mock_protocol()
    connect_gate = asyncio.Event()

    async def _gated_connect():
        await connect_gate.wait()

    p1.connect.side_effect = _gated_connect
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")}, protocols={"d1": p1}
    )
    start_task = asyncio.create_task(runtime.start())
    try:
        await asyncio.sleep(0)  # 让 start 进入 connect 等待
        assert runtime.running is False
    finally:
        connect_gate.set()
        await start_task
        assert runtime.running is True
        await runtime.stop()


# ---------------------------------------------------------------------------
# 设备热管理
# ---------------------------------------------------------------------------


async def test_add_device_connects_and_registers_job() -> None:
    runtime, engine, scheduler = _make_runtime()
    await runtime.start()
    try:
        p_new = _make_mock_protocol()
        cfg = _make_device("d2", polling_interval=5.0)
        points = [_make_point("d2", "p1")]
        await runtime.add_device("d2", cfg, p_new, points)

        assert runtime.devices["d2"] is cfg
        assert runtime.protocols["d2"] is p_new
        assert runtime.points_by_device["d2"] == points
        p_new.set_points_mapping.assert_called_once_with(points)
        p_new.connect.assert_awaited_once()
        assert "poll:d2:default" in scheduler.jobs
        # 引擎与 Runtime 共享注册表——新设备立即可采集
        assert engine._protocols["d2"] is p_new  # noqa: SLF001
    finally:
        await runtime.stop()


async def test_add_device_connect_failure_still_registers_job() -> None:
    runtime, _engine, scheduler = _make_runtime()
    await runtime.start()
    try:
        p_new = _make_mock_protocol()
        p_new.connect.side_effect = OSError("unreachable")
        await runtime.add_device("d2", _make_device("d2"), p_new, [])
        assert "poll:d2:default" in scheduler.jobs
    finally:
        await runtime.stop()


async def test_remove_device_unregisters_jobs_and_closes() -> None:
    p1 = _make_mock_protocol()
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("d1", "p1")]},
    )
    await runtime.start()
    try:
        assert "poll:d1:default" in scheduler.jobs
        await runtime.remove_device("d1")

        assert scheduler.jobs == {}
        p1.close.assert_awaited_once()
        assert "d1" not in runtime.devices
        assert "d1" not in runtime.protocols
        assert "d1" not in runtime.points_by_device
    finally:
        await runtime.stop()


async def test_remove_unknown_device_is_noop() -> None:
    runtime, _engine, _scheduler = _make_runtime()
    await runtime.start()
    try:
        await runtime.remove_device("ghost")  # 不抛异常
    finally:
        await runtime.stop()


async def test_rebuild_device_swaps_protocol_and_restarts_jobs() -> None:
    p_old = _make_mock_protocol()
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", polling_interval=1.0)},
        protocols={"d1": p_old},
        points_by_device={"d1": [_make_point("d1", "p1")]},
    )
    await runtime.start()
    try:
        p_new = _make_mock_protocol()
        new_cfg = _make_device("d1", polling_interval=3.0)
        new_points = [_make_point("d1", "p9")]
        await runtime.rebuild_device("d1", new_cfg, p_new, new_points)

        p_old.close.assert_awaited_once()
        assert runtime.protocols["d1"] is p_new
        assert runtime.points_by_device["d1"] == new_points
        p_new.connect.assert_awaited_once()
        interval, _func, _args = scheduler.jobs["poll:d1:default"]
        assert interval == 3.0
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# sink 热管理
# ---------------------------------------------------------------------------


async def test_add_sink_opens_and_starts_consumer() -> None:
    runtime, _engine, _scheduler = _make_runtime()
    await runtime.start()
    try:
        s_new = _make_mock_sink()
        await runtime.add_sink("s2", SinkConfig(name="s2", type="file"), s_new)

        s_new.open.assert_awaited_once()
        assert "s2" in runtime._queues  # noqa: SLF001
        assert "s2" in runtime._sink_tasks  # noqa: SLF001
        assert runtime.sink_count == 1
    finally:
        await runtime.stop()


async def test_add_sink_open_failure_propagates() -> None:
    """热新增 sink 打开失败原样上抛，由编排方（reconfigure）记录错误。"""
    runtime, _engine, _scheduler = _make_runtime()
    await runtime.start()
    try:
        bad = _make_mock_sink()
        bad.open.side_effect = OSError("cannot open")
        with pytest.raises(OSError, match="cannot open"):
            await runtime.add_sink("s2", SinkConfig(name="s2", type="file"), bad)
    finally:
        await runtime.stop()


async def test_remove_sink_stops_consumer_and_closes() -> None:
    s1 = _make_mock_sink()
    runtime, _engine, _scheduler = _make_runtime(sinks={"s1": s1})
    await runtime.start()
    try:
        await runtime.remove_sink("s1")

        s1.flush.assert_awaited_once()
        s1.close.assert_awaited_once()
        assert "s1" not in runtime._queues  # noqa: SLF001
        assert "s1" not in runtime._sink_tasks  # noqa: SLF001
        assert runtime.sink_count == 0
    finally:
        await runtime.stop()


async def test_rebuild_sink_preserves_queue() -> None:
    """重建 sink 保留既有队列——在途数据不丢失。"""
    s_old = _make_mock_sink()
    runtime, _engine, _scheduler = _make_runtime(sinks={"s1": s_old})
    await runtime.start()
    try:
        queue_before = runtime._queues["s1"]  # noqa: SLF001
        s_new = _make_mock_sink()
        await runtime.rebuild_sink("s1", SinkConfig(name="s1", type="file"), s_new)

        assert runtime._queues["s1"] is queue_before  # noqa: SLF001
        s_old.flush.assert_awaited_once()
        s_old.close.assert_awaited_once()
        s_new.open.assert_awaited_once()
        assert "s1" in runtime._sink_tasks  # noqa: SLF001
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# router / pipeline 替换
# ---------------------------------------------------------------------------


async def test_replace_router_delegates_to_engine() -> None:
    runtime, engine, _scheduler = _make_runtime()
    new_router = MagicMock(spec=Router)
    new_router.table_size = 3
    await runtime.replace_router(new_router)
    assert engine.current_router is new_router
    assert runtime.current_router is new_router


async def test_replace_pipeline_delegates_to_engine() -> None:
    runtime, engine, _scheduler = _make_runtime()
    new_pipeline = Pipeline([])
    await runtime.replace_pipeline(new_pipeline)
    assert engine._pipeline is new_pipeline  # noqa: SLF001


# ---------------------------------------------------------------------------
# reconfigure —— 热重载编排
# ---------------------------------------------------------------------------


async def test_reconfigure_applies_device_diff_via_factory() -> None:
    p1 = _make_mock_protocol()
    p_new = _make_mock_protocol()
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("d1", "p1")]},
        protocol_factory=lambda cfg: p_new,
    )
    await runtime.start()
    try:
        new_cfg = _make_full_config(devices=[_make_device("d2", polling_interval=2.0)])
        diff = ConfigDiff(
            devices=DeviceDiff(added=["d2"], removed=["d1"]),
        )
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        assert "d1" not in runtime.devices
        assert "d2" in runtime.devices
        assert "poll:d1:default" not in scheduler.jobs
        assert "poll:d2:default" in scheduler.jobs
    finally:
        await runtime.stop()


async def test_reconfigure_applies_sink_diff_via_factory() -> None:
    s_old = _make_mock_sink()
    s_new = _make_mock_sink()
    new_sink_cfg = SinkConfig(name="s2", type="file")
    runtime, _engine, _scheduler = _make_runtime(
        sinks={"s1": s_old},
        sink_factory=lambda cfg: s_new,
    )
    await runtime.start()
    try:
        new_cfg = _make_full_config(sinks=[new_sink_cfg])
        diff = ConfigDiff(sinks=SinkDiff(added=["s2"], removed=["s1"]))
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        assert runtime.sink_count == 1
        assert "s2" in runtime._queues  # noqa: SLF001
        s_old.close.assert_awaited_once()
        s_new.open.assert_awaited_once()
    finally:
        await runtime.stop()


async def test_reconfigure_rebuilds_router_on_points_change() -> None:
    runtime, engine, _scheduler = _make_runtime()
    await runtime.start()
    try:
        router_before = engine.current_router
        new_cfg = _make_full_config(points=[_make_point("d1", "p1")])
        diff = ConfigDiff(points_changed=True)
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        assert engine.current_router is not router_before
    finally:
        await runtime.stop()


async def test_reconfigure_rebuilds_pipeline_via_processor_factory() -> None:
    processor = MagicMock()
    processor_factory = MagicMock(return_value=processor)
    runtime, engine, _scheduler = _make_runtime(processor_factory=processor_factory)
    await runtime.start()
    try:
        pipeline_before = engine._pipeline  # noqa: SLF001
        new_cfg = _make_full_config()
        diff = ConfigDiff(pipeline_changed=True)
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        assert engine._pipeline is not pipeline_before  # noqa: SLF001
    finally:
        await runtime.stop()


async def test_reconfigure_without_factories_reports_errors() -> None:
    """工厂未接线时不得静默——错误进入返回列表，其余阶段继续。"""
    runtime, engine, _scheduler = _make_runtime()  # 无工厂
    await runtime.start()
    try:
        router_before = engine.current_router
        new_cfg = _make_full_config(
            devices=[_make_device("d2")],
            sinks=[SinkConfig(name="s2", type="file")],
        )
        diff = ConfigDiff(
            devices=DeviceDiff(added=["d2"]),
            sinks=SinkDiff(added=["s2"]),
            points_changed=True,
        )
        errors = await runtime.reconfigure(new_cfg, diff)

        assert any(e.startswith("device:") for e in errors)
        assert any(e.startswith("sink:") for e in errors)
        # 路由重建不依赖工厂——即使设备/sink 阶段失败也应执行
        assert engine.current_router is not router_before
    finally:
        await runtime.stop()


async def test_reconfigure_remove_only_needs_no_factory() -> None:
    """纯删除 diff 不触发工厂——未接线工厂的运行时也能缩容。"""
    p1 = _make_mock_protocol()
    s1 = _make_mock_sink()
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        sinks={"s1": s1},
    )
    await runtime.start()
    try:
        diff = ConfigDiff(
            devices=DeviceDiff(removed=["d1"]),
            sinks=SinkDiff(removed=["s1"]),
        )
        errors = await runtime.reconfigure(_make_full_config(), diff)

        assert errors == []
        assert runtime.device_count == 0
        assert runtime.sink_count == 0
        assert scheduler.jobs == {}
    finally:
        await runtime.stop()


async def test_reconfigure_no_changes_is_noop() -> None:
    runtime, engine, _scheduler = _make_runtime()
    await runtime.start()
    try:
        router_before = engine.current_router
        errors = await runtime.reconfigure(_make_full_config(), ConfigDiff())
        assert errors == []
        assert engine.current_router is router_before
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# 背压与计数（决策 7）
# ---------------------------------------------------------------------------


async def test_backpressure_drop_new_counts_dropped() -> None:
    runtime, _engine, _scheduler = _make_runtime(config=_make_config("drop_new"))
    queue: asyncio.Queue[list[PointValue]] = asyncio.Queue(maxsize=1)
    await queue.put([_value()])  # 占满

    await runtime._handle_backpressure(queue, [_value(), _value()], "s1")  # noqa: SLF001

    assert runtime.points_dropped == 2
    assert runtime.points_routed == 0
    assert queue.qsize() == 1


async def test_backpressure_drop_old_evicts_and_counts() -> None:
    runtime, _engine, _scheduler = _make_runtime(config=_make_config("drop_old"))
    queue: asyncio.Queue[list[PointValue]] = asyncio.Queue(maxsize=1)
    await queue.put([_value(), _value()])  # 占满（2 点旧批次）

    await runtime._handle_backpressure(queue, [_value()], "s1")  # noqa: SLF001

    assert runtime.points_dropped == 2
    assert runtime.points_routed == 1
    assert queue.qsize() == 1


async def test_backpressure_block_waits_for_space() -> None:
    runtime, _engine, _scheduler = _make_runtime(config=_make_config("block"))
    queue: asyncio.Queue[list[PointValue]] = asyncio.Queue(maxsize=1)
    await queue.put([_value()])

    put_task = asyncio.create_task(
        runtime._handle_backpressure(queue, [_value()], "s1")  # noqa: SLF001
    )
    await asyncio.sleep(0)
    assert not put_task.done()  # 队列满——阻塞等待

    await queue.get()  # 腾出空间
    await asyncio.wait_for(put_task, timeout=2.0)
    assert runtime.points_routed == 1


async def test_dispatch_ignores_unknown_sink_and_empty_batch() -> None:
    runtime, _engine, _scheduler = _make_runtime()
    await runtime.dispatch({"ghost": [_value()], "s1": []})  # 不抛异常
    assert runtime.points_routed == 0
