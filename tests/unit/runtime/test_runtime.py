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
    PointTableConfig,
    PointTablesConfig,
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
from wind_hub.domain.model.point import PointValue, Quality
from wind_hub.domain.model.reload import ConfigDiff, DeviceDiff, SinkDiff
from wind_hub.domain.model.route import DeliveryConfig, RouteRule, RouteTarget
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
    point_table: str = "t1",
    polling: list[PollingGroup] | None = None,
    read_mode: str = "sum",
) -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol="modbus",
        point_table=point_table,
        endpoint=Endpoint(host="10.0.0.1", port=502),
        polling=(
            polling
            if polling is not None
            else [PollingGroup(group="default", interval=polling_interval)]
        ),
        enabled=True,
        mode=mode,
        read_mode=read_mode,
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


def _make_point(point_id: str, group: str = "default") -> PointConfig:
    return PointConfig(
        point_id=point_id,
        group=group,
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
    clock: Callable[[], float] | None = None,
) -> tuple[Runtime, AcquisitionEngine, _FakeScheduler]:
    """构造 Runtime + 真实引擎 + Fake 调度器，返回三元组。

    ``clock`` 可注入假时钟（如 ``lambda: now[0]``）以确定性验证
    重连 backoff——避免真实 ``asyncio.sleep`` 等待。
    """
    protocols = protocols if protocols is not None else {}
    points_by_device = points_by_device if points_by_device is not None else {}
    effective_config = config or _make_config()
    engine = AcquisitionEngine(
        protocols=protocols,
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        points_by_device=points_by_device,
        # 与组合根（assembly）一致：引擎的应用层读超时取自系统配置。
        read_timeout=effective_config.read_timeout,
    )
    scheduler = _FakeScheduler()
    kwargs = {"clock": clock} if clock is not None else {}
    runtime = Runtime(
        devices=devices if devices is not None else {},
        protocols=protocols,
        sinks=sinks if sinks is not None else {},
        engine=engine,
        scheduler=scheduler,
        dispatcher=MagicMock(spec=Dispatcher),
        config=effective_config,
        points_by_device=points_by_device,
        protocol_factory=protocol_factory,
        sink_factory=sink_factory,
        processor_factory=processor_factory,
        **kwargs,  # type: ignore[arg-type]
    )
    return runtime, engine, scheduler


def _make_full_config(
    devices: list[DeviceConfig] | None = None,
    sinks: list[SinkConfig] | None = None,
    points: list[PointConfig] | None = None,
    tables: dict[str, list[PointConfig]] | None = None,
    rules: list[RouteRule] | None = None,
) -> Config:
    """构造完整 Config；``points`` 是表 ``t1`` 的便捷写法，``tables`` 显式给多表。"""
    table_map = (
        {name: PointTableConfig(points=pts) for name, pts in tables.items()}
        if tables is not None
        else {"t1": PointTableConfig(points=points or [])}
    )
    return Config(
        system=SystemConfig(sinks=sinks or [], pipeline=PipelineConfig(processors=[])),
        devices=DevicesConfig(devices=devices or []),
        point_tables=PointTablesConfig(tables=table_map),
        routing=RoutingConfig(rules=rules or []),
    )


# ---------------------------------------------------------------------------
# start() —— 设备连接 / 点表注入 / Job 注册
# ---------------------------------------------------------------------------


async def test_start_injects_points_connects_and_registers_jobs() -> None:
    p1 = _make_mock_protocol()
    points = {"d1": [_make_point("p1")]}
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


async def test_sequential_read_mode_device_polls_via_normal_job() -> None:
    """read_mode=sequential + mode=poll：Job 注册与 collect 与 sum 设备完全
    同构——引擎与 SchedulerPort 不理解 read_mode，读取方式由驱动内部分派。"""
    p1 = _make_mock_protocol()
    p1.read.return_value = [_value()]
    runtime, engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", read_mode="sequential")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
    )
    try:
        await runtime.start()
        assert "poll:d1:default" in scheduler.jobs  # 周期 Job 照常注册
        await scheduler.trigger_job("poll:d1:default")
        p1.read.assert_awaited_once()  # collect 正常执行
        assert engine.points_collected == 1
    finally:
        await runtime.stop()


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
        points_by_device={"d1": [_make_point("p1")]},
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
        points_by_device={"d1": [_make_point("p1")]},
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
        points_by_device={"d1": [_make_point("p1")]},
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
        points = [_make_point("p1")]
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
        points_by_device={"d1": [_make_point("p1")]},
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
        points_by_device={"d1": [_make_point("p1")]},
    )
    await runtime.start()
    try:
        p_new = _make_mock_protocol()
        new_cfg = _make_device("d1", polling_interval=3.0)
        new_points = [_make_point("p9")]
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
        points_by_device={"d1": [_make_point("p1")]},
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
        new_cfg = _make_full_config(points=[_make_point("p1")])
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


# ---------------------------------------------------------------------------
# 热重载——轻量设备更新与点表重注入（A.10）
# ---------------------------------------------------------------------------


async def test_polling_interval_change_replaces_only_that_job() -> None:
    """polling interval 变化：只重建受影响 Job，不重建 Protocol 连接。"""
    p1 = _make_mock_protocol()
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", polling_interval=1.0)},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
    )
    await runtime.start()
    try:
        p1.connect.reset_mock()
        new_cfg = _make_full_config(
            devices=[_make_device("d1", polling_interval=3.0)],
            points=[_make_point("p1")],
        )
        diff = ConfigDiff(devices=DeviceDiff(updated=["d1"]))
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        p1.connect.assert_not_awaited()  # 未重连
        p1.close.assert_not_awaited()
        interval, _func, _args = scheduler.jobs["poll:d1:default"]
        assert interval == 3.0
    finally:
        await runtime.stop()


async def test_polling_group_removed_only_removes_that_job() -> None:
    """polling group 删除：仅注销对应 Job，其余 Job 与连接不受影响。"""
    p1 = _make_mock_protocol()
    two_groups = [
        PollingGroup(group="fast", interval=1.0),
        PollingGroup(group="slow", interval=10.0),
    ]
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", polling=two_groups)},
        protocols={"d1": p1},
        points_by_device={
            "d1": [_make_point("p1", group="fast"), _make_point("p2", group="slow")]
        },
    )
    await runtime.start()
    try:
        assert "poll:d1:fast" in scheduler.jobs
        assert "poll:d1:slow" in scheduler.jobs
        p1.connect.reset_mock()

        new_cfg = _make_full_config(
            devices=[_make_device("d1", polling=[PollingGroup(group="fast", interval=1.0)])],
            points=[_make_point("p1", group="fast")],
        )
        diff = ConfigDiff(devices=DeviceDiff(updated=["d1"]))
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        p1.connect.assert_not_awaited()
        assert "poll:d1:fast" in scheduler.jobs
        assert "poll:d1:slow" not in scheduler.jobs
    finally:
        await runtime.stop()


async def test_polling_group_added_only_adds_that_job() -> None:
    """polling group 新增：仅注册新 Job——不存在逐点 Job。"""
    p1 = _make_mock_protocol()
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", polling=[PollingGroup(group="fast", interval=1.0)])},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1", group="fast")]},
    )
    await runtime.start()
    try:
        p1.connect.reset_mock()
        two_groups = [
            PollingGroup(group="fast", interval=1.0),
            PollingGroup(group="slow", interval=10.0),
        ]
        new_cfg = _make_full_config(
            devices=[_make_device("d1", polling=two_groups)],
            points=[_make_point("p1", group="fast"), _make_point("p2", group="slow")],
        )
        diff = ConfigDiff(devices=DeviceDiff(updated=["d1"]))
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        p1.connect.assert_not_awaited()
        # 一个 (device, group) 一个 Job——永远不存在逐点 Job
        assert set(scheduler.jobs) == {"poll:d1:fast", "poll:d1:slow"}
        interval, func, args = scheduler.jobs["poll:d1:slow"]
        assert interval == 10.0
        assert args == ("d1", "slow")
    finally:
        await runtime.stop()


async def test_point_table_rebind_reinjects_mapping_without_reconnect() -> None:
    """point_table 换绑：仅向既有 Protocol 重注入新表映射，不重建连接。"""
    p1 = _make_mock_protocol()
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1", point_table="t1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
    )
    await runtime.start()
    try:
        p1.connect.reset_mock()
        p1.set_points_mapping.reset_mock()
        t2_points = [_make_point("p9")]
        new_cfg = _make_full_config(
            devices=[_make_device("d1", point_table="t2")],
            tables={"t1": [_make_point("p1")], "t2": t2_points},
        )
        diff = ConfigDiff(devices=DeviceDiff(updated=["d1"]))
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        p1.connect.assert_not_awaited()
        p1.set_points_mapping.assert_called_once_with(t2_points)
        assert runtime.points_by_device["d1"] == t2_points
    finally:
        await runtime.stop()


async def test_endpoint_change_rebuilds_device() -> None:
    """连接参数变化：走重建路径（关闭旧连接、工厂创建新驱动）。"""
    p_old = _make_mock_protocol()
    p_new = _make_mock_protocol()
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p_old},
        points_by_device={"d1": [_make_point("p1")]},
        protocol_factory=lambda cfg: p_new,
    )
    await runtime.start()
    try:
        changed = _make_device("d1")
        changed.endpoint = Endpoint(host="10.0.9.9", port=502)
        new_cfg = _make_full_config(devices=[changed], points=[_make_point("p1")])
        diff = ConfigDiff(devices=DeviceDiff(updated=["d1"]))
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        p_old.close.assert_awaited_once()
        p_new.connect.assert_awaited_once()
        assert runtime.protocols["d1"] is p_new
    finally:
        await runtime.stop()


async def test_table_content_change_reinjects_mapping_without_reconnect() -> None:
    """点表内容变化（设备未变）：重注入点映射，不重建 Protocol 连接。"""
    p1 = _make_mock_protocol()
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
    )
    await runtime.start()
    try:
        p1.connect.reset_mock()
        p1.set_points_mapping.reset_mock()
        new_points = [_make_point("p1"), _make_point("p2")]
        new_cfg = _make_full_config(
            devices=[_make_device("d1")],
            points=new_points,
        )
        diff = ConfigDiff(points_changed=True, point_tables_changed=["t1"])
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        p1.connect.assert_not_awaited()
        p1.set_points_mapping.assert_called_once_with(new_points)
        assert runtime.points_by_device["d1"] == new_points
    finally:
        await runtime.stop()


async def test_point_table_hot_reload_does_not_mutate_old_snapshot() -> None:
    """点表快照语义：热重载整体替换，此前持有的点表 list（旧快照）
    不被原地修改——消费者可安全持有旧引用直至处理完成。"""
    p1 = _make_mock_protocol()
    old_points = [_make_point("p1")]
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": old_points},
    )
    await runtime.start()
    try:
        held_snapshot = runtime.points_by_device["d1"]
        new_cfg = _make_full_config(
            devices=[_make_device("d1")],
            points=[_make_point("p1"), _make_point("p2")],
        )
        diff = ConfigDiff(points_changed=True, point_tables_changed=["t1"])
        errors = await runtime.reconfigure(new_cfg, diff)

        assert errors == []
        assert held_snapshot == old_points  # 旧快照内容未变
        assert len(held_snapshot) == 1  # 未被原地追加 p2
        assert len(runtime.points_by_device["d1"]) == 2  # 新快照是新内容
    finally:
        await runtime.stop()


async def test_multi_group_device_registers_one_job_per_group() -> None:
    """多分组设备：每个 (device, group) 恰好一个 Job，id 为 poll:{device}:{group}。"""
    p1 = _make_mock_protocol()
    groups = [
        PollingGroup(group="fast", interval=1.0),
        PollingGroup(group="slow", interval=10.0),
    ]
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", polling=groups)},
        protocols={"d1": p1},
        points_by_device={
            "d1": [_make_point("p1", group="fast"), _make_point("p2", group="slow")]
        },
    )
    try:
        await runtime.start()
        assert set(scheduler.jobs) == {"poll:d1:fast", "poll:d1:slow"}
        assert scheduler.jobs["poll:d1:fast"][0] == 1.0
        assert scheduler.jobs["poll:d1:slow"][0] == 10.0
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# 投递策略热替换（阶段 B / B.4）
# ---------------------------------------------------------------------------


async def test_reconfigure_rebuilds_delivery_on_rules_change() -> None:
    """规则（含 delivery）变化 → 路由重建阶段同时替换 DeliveryDispatcher。"""
    runtime, engine, _scheduler = _make_runtime()
    await runtime.start()
    try:
        assert engine._delivery is None  # noqa: SLF001 — 初始未装配策略
        new_cfg = _make_full_config(
            devices=[_make_device("d1")],
            points=[_make_point("p1")],
            rules=[
                RouteRule(
                    name="r1",
                    targets=[RouteTarget(sink="s1", delivery=DeliveryConfig(type="every_n", n=2))],
                )
            ],
        )
        errors = await runtime.reconfigure(new_cfg, ConfigDiff(rules_changed=True))

        assert errors == []
        delivery = engine._delivery  # noqa: SLF001
        assert delivery is not None
        # 策略真实生效：every_n=2 → 第 2 批被抑制
        routed = {"s1": [PointValue(device_id="d1", point_id="p1", value=1.0)]}
        assert delivery.evaluate(routed) == routed
        assert delivery.evaluate(routed) == {}
    finally:
        await runtime.stop()


async def test_delivery_policy_change_rebuilds_neither_sink_nor_protocol() -> None:
    """B.4：仅投递策略变化时，不重建 Sink、不重连 Protocol。"""
    p1 = _make_mock_protocol()
    s1 = _make_mock_sink()
    points = {"d1": [_make_point("p1")]}
    runtime, engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        sinks={"s1": s1},
        points_by_device=points,
    )
    await runtime.start()
    try:
        p1.connect.reset_mock()
        s1.open.reset_mock()
        delivery_before = engine._delivery  # noqa: SLF001

        new_cfg = _make_full_config(
            devices=[_make_device("d1")],
            sinks=[SinkConfig(name="s1", type="file")],
            points=[_make_point("p1")],
            rules=[
                RouteRule(
                    name="r1",
                    targets=[RouteTarget(sink="s1", delivery=DeliveryConfig(type="on_change"))],
                )
            ],
        )
        errors = await runtime.reconfigure(new_cfg, ConfigDiff(rules_changed=True))

        assert errors == []
        assert engine._delivery is not delivery_before  # noqa: SLF001
        p1.connect.assert_not_awaited()  # 无 Protocol 重连
        s1.open.assert_not_awaited()  # 无 Sink 重建
        assert runtime._sinks["s1"] is s1  # noqa: SLF001 — sink 实例原样保留
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# 采集失败与设备恢复（故障分类 / backoff 重连 / DeviceRuntimeState）
#
# 全程使用假时钟推进 backoff 窗口——不做真实 asyncio.sleep 等待。
# ---------------------------------------------------------------------------


def _fake_clock(start: float = 1000.0) -> tuple[list[float], Callable[[], float]]:
    """返回 ``([当前时刻], 时钟函数)``——测试通过改写列表元素推进时间。"""
    now = [start]
    return now, (lambda: now[0])


async def test_read_failure_does_not_remove_periodic_job() -> None:
    """单次读失败只记 warning——周期 Job 保留，下个周期自然重试。"""
    p1 = _make_mock_protocol()
    p1.read.side_effect = OSError("device busy")
    runtime, engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
    )
    try:
        await runtime.start()
        await scheduler.trigger_job("poll:d1:default")  # 读失败——不抛、不摘 Job
        assert "poll:d1:default" in scheduler.jobs
        assert engine.points_collected == 0

        p1.read.side_effect = None
        p1.read.return_value = [_value()]
        await scheduler.trigger_job("poll:d1:default")  # 下个周期恢复
        assert engine.points_collected == 1
    finally:
        await runtime.stop()


async def test_device_down_at_start_does_not_stop_runtime() -> None:
    """设备连接失败 ≠ Runtime 停止：running 仍为 True，状态记入 DeviceRuntimeState。"""
    now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.connect.side_effect = OSError("refused")
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1")}, protocols={"d1": p1}, clock=clock
    )
    try:
        await runtime.start()
        assert runtime.running is True

        state = runtime.device_state("d1")
        assert state is not None
        assert state.connected is False
        assert state.consecutive_failures == 1
        assert "refused" in (state.last_error or "")
        assert state.next_retry_at == now[0] + 1.0  # 初始 backoff 1s
    finally:
        await runtime.stop()


async def test_disconnected_collect_skips_read_without_connect_storm() -> None:
    """断线且未到重试窗口：collect 跳过读，也不发起 connect——
    1 Hz 轮询不会形成每秒一次的重连风暴。"""
    now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.connect.side_effect = OSError("refused")
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        clock=clock,
    )
    try:
        await runtime.start()
        assert p1.connect.await_count == 1  # start 时的一次尝试

        await scheduler.trigger_job("poll:d1:default")  # 仍在节流窗口内
        assert p1.connect.await_count == 1  # 没有新的 connect 尝试
        p1.read.assert_not_awaited()  # 也没有读

        now[0] += 0.5  # 0.5s < 1s 窗口——仍然节流
        await scheduler.trigger_job("poll:d1:default")
        assert p1.connect.await_count == 1
    finally:
        await runtime.stop()


async def test_backoff_grows_exponentially_and_caps_at_30s() -> None:
    """重连节流：T_k = min(30, 1·2^k)——1,2,4,8,16,30,30… 无 jitter。"""
    now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.connect.side_effect = OSError("refused")
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        clock=clock,
    )
    try:
        await runtime.start()  # 第 1 次失败（start 阶段）
        expected_delays = [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        for failures, delay in enumerate(expected_delays, start=2):
            state = runtime.device_state("d1")
            assert state is not None
            now[0] = state.next_retry_at  # 推进到节流窗口
            await scheduler.trigger_job("poll:d1:default")  # 触发一次重连尝试
            state = runtime.device_state("d1")
            assert state is not None
            assert state.consecutive_failures == failures
            assert state.next_retry_at == now[0] + delay
        assert p1.connect.await_count == 1 + len(expected_delays)
    finally:
        await runtime.stop()


async def test_successful_reconnect_resets_state_and_recovers_collect() -> None:
    """重连成功后失败计数清零，collect 恢复正常读取。"""
    now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.connect.side_effect = OSError("refused")
    runtime, engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        clock=clock,
    )
    try:
        await runtime.start()  # 失败 1 次
        now[0] += 1.0
        await scheduler.trigger_job("poll:d1:default")  # 失败 2 次
        state = runtime.device_state("d1")
        assert state is not None
        assert state.consecutive_failures == 2

        # 设备恢复：推进到下一个窗口，connect 成功
        p1.connect.side_effect = None
        p1.read.return_value = [_value()]
        now[0] = state.next_retry_at
        await scheduler.trigger_job("poll:d1:default")

        state = runtime.device_state("d1")
        assert state is not None
        assert state.connected is True
        assert state.consecutive_failures == 0
        assert state.last_error is None
        assert engine.points_collected == 1  # 本次 collect 真实读到了数据
    finally:
        await runtime.stop()


async def test_connection_level_read_failure_triggers_reconnect_on_next_collect() -> None:
    """运行期连接级读失败（如超时）：标记断线，下次 collect 先重连再读。"""
    _now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.read.return_value = [_value()]
    runtime, engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        clock=clock,
    )
    try:
        await runtime.start()
        await scheduler.trigger_job("poll:d1:default")
        assert engine.points_collected == 1

        # 传输断开：读抛连接级异常 → 状态转为断线
        p1.read.side_effect = TimeoutError("read timeout")
        await scheduler.trigger_job("poll:d1:default")
        state = runtime.device_state("d1")
        assert state is not None
        assert state.connected is False
        assert "timeout" in (state.last_error or "")

        # 下次 collect：ensure_connected 先重连（驱动 connect 幂等），随后读恢复
        p1.read.side_effect = None
        p1.connect.reset_mock()
        await scheduler.trigger_job("poll:d1:default")
        p1.connect.assert_awaited_once()
        assert engine.points_collected == 2
        state = runtime.device_state("d1")
        assert state is not None
        assert state.connected is True
    finally:
        await runtime.stop()


async def test_non_connection_read_failure_keeps_connected() -> None:
    """协议/编程级读失败（非连接级）：只记录错误，不触发断线与重连。"""
    _now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.read.side_effect = ValueError("bad register map")
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        clock=clock,
    )
    try:
        await runtime.start()
        await scheduler.trigger_job("poll:d1:default")

        state = runtime.device_state("d1")
        assert state is not None
        assert state.connected is True  # 连接本身仍健康
        assert "bad register map" in (state.last_error or "")

        # 下次 collect 直接读，不走重连路径
        p1.connect.reset_mock()
        await scheduler.trigger_job("poll:d1:default")
        p1.connect.assert_not_awaited()
    finally:
        await runtime.stop()


async def test_one_device_failure_isolated_from_others() -> None:
    """单台设备故障不影响其它设备：故障设备节流跳过，健康设备照常采集。"""
    _now, clock = _fake_clock()
    p_bad = _make_mock_protocol()
    p_bad.connect.side_effect = OSError("refused")
    p_good = _make_mock_protocol()
    p_good.read.return_value = [_value("d2", "p1")]
    runtime, engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1"), "d2": _make_device("d2")},
        protocols={"d1": p_bad, "d2": p_good},
        points_by_device={"d1": [_make_point("p1")], "d2": [_make_point("p1")]},
        clock=clock,
    )
    try:
        await runtime.start()
        await scheduler.trigger_job("poll:d1:default")  # 故障设备：节流跳过
        await scheduler.trigger_job("poll:d2:default")  # 健康设备：正常采集

        assert engine.points_collected == 1
        p_good.read.assert_awaited_once()
        p_bad.read.assert_not_awaited()

        good_state = runtime.device_state("d2")
        assert good_state is not None
        assert good_state.connected is True
        assert good_state.consecutive_failures == 0
        assert runtime.running is True
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# AcquisitionRuntimeState —— 采集 Job 业务执行状态（与设备连接/调度 Job 分维度）
#
# 全程使用假时钟验证时间字段——不做真实 asyncio.sleep 等待。
# ---------------------------------------------------------------------------


async def test_acq_state_first_success_records_lifecycle() -> None:
    """首次成功采集：running 开始/结束翻转、last_success_at/last_duration 记录、
    失败计数与错误保持清零/空。"""
    now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.read.return_value = [_value()]
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        clock=clock,
    )
    try:
        await runtime.start()
        states = runtime.acquisition_states()
        # Job 注册即建立状态（首次 collect 前 status 即可见）
        assert "poll:d1:default" in states
        state = states["poll:d1:default"]
        assert state.running is False
        assert state.last_success_at is None

        await scheduler.trigger_job("poll:d1:default")

        state = runtime.acquisition_states()["poll:d1:default"]
        assert state.running is False  # finally 归位
        assert state.last_started_at == now[0]
        assert state.last_finished_at == now[0]
        assert state.last_success_at == now[0]
        assert state.last_duration == pytest.approx(0.0)
        assert state.consecutive_failures == 0
        assert state.last_error is None
    finally:
        await runtime.stop()


async def test_acq_state_consecutive_failures_then_success_resets() -> None:
    """连续失败累加 consecutive_failures 与 last_error；成功后清零、错误清空。"""
    now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.read.side_effect = OSError("device busy")
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        clock=clock,
    )
    try:
        await runtime.start()
        await scheduler.trigger_job("poll:d1:default")
        now[0] += 1.0
        await scheduler.trigger_job("poll:d1:default")

        state = runtime.acquisition_states()["poll:d1:default"]
        assert state.consecutive_failures == 2
        assert "device busy" in (state.last_error or "")
        assert state.last_success_at is None
        assert state.last_duration == pytest.approx(0.0)  # 第二次 collect 耗时
        assert state.last_finished_at == now[0]

        # 恢复：成功一次 → 计数清零、错误清空、记录成功时刻
        p1.read.side_effect = None
        p1.read.return_value = [_value()]
        now[0] += 1.0
        await scheduler.trigger_job("poll:d1:default")

        state = runtime.acquisition_states()["poll:d1:default"]
        assert state.consecutive_failures == 0
        assert state.last_error is None
        assert state.last_success_at == now[0]
    finally:
        await runtime.stop()


async def test_acq_state_partial_does_not_count_as_failure() -> None:
    """PARTIAL（GOOD/BAD 混合）：计为成功——不累加连续失败。"""
    _now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.read.return_value = [
        _value(point_id="p1"),
        PointValue(device_id="d1", point_id="p2", value=None, quality=Quality.BAD),
    ]
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1"), _make_point("p2")]},
        clock=clock,
    )
    try:
        await runtime.start()
        # 先制造一次真失败，再 partial——partial 必须把它清零
        p1.read.side_effect = OSError("boom")
        await scheduler.trigger_job("poll:d1:default")
        assert (
            runtime.acquisition_states()["poll:d1:default"].consecutive_failures == 1
        )

        p1.read.side_effect = None
        await scheduler.trigger_job("poll:d1:default")

        state = runtime.acquisition_states()["poll:d1:default"]
        assert state.consecutive_failures == 0
        assert state.last_error is None
    finally:
        await runtime.stop()


async def test_acq_state_isolated_per_group_and_device() -> None:
    """多 group / 多设备的状态互相隔离：一个失败不影响其它 Job 的状态。"""
    _now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.read.return_value = [_value()]
    p2 = _make_mock_protocol()
    p2.read.side_effect = OSError("d2 down")
    two_groups = [
        PollingGroup(group="fast", interval=1.0),
        PollingGroup(group="slow", interval=10.0),
    ]
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", polling=two_groups), "d2": _make_device("d2")},
        protocols={"d1": p1, "d2": p2},
        points_by_device={
            "d1": [_make_point("p1", group="fast"), _make_point("p2", group="slow")],
            "d2": [_make_point("p1")],
        },
        clock=clock,
    )
    try:
        await runtime.start()
        await scheduler.trigger_job("poll:d1:fast")  # 成功
        await scheduler.trigger_job("poll:d2:default")  # 失败

        states = runtime.acquisition_states()
        assert set(states) == {"poll:d1:fast", "poll:d1:slow", "poll:d2:default"}
        assert states["poll:d1:fast"].consecutive_failures == 0
        assert states["poll:d1:fast"].last_success_at is not None
        # 未执行的 slow Job：零状态
        assert states["poll:d1:slow"].last_started_at is None
        assert states["poll:d1:slow"].consecutive_failures == 0
        # d2 的失败不串到 d1
        assert states["poll:d2:default"].consecutive_failures == 1
        assert "d2 down" in (states["poll:d2:default"].last_error or "")
    finally:
        await runtime.stop()


async def test_acq_state_disconnected_skip_marks_failed_keeps_job() -> None:
    """断线跳过一次采集：acq 记 FAILED（last_error 指明断线/backoff），
    周期 Job 保留，Runtime.running 不受影响。"""
    now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.connect.side_effect = OSError("refused")
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        clock=clock,
    )
    try:
        await runtime.start()  # 连接失败 → 断线 + 节流窗口
        await scheduler.trigger_job("poll:d1:default")  # 节流窗口内：跳过读

        assert "poll:d1:default" in scheduler.jobs  # Job 保留
        assert runtime.running is True
        state = runtime.acquisition_states()["poll:d1:default"]
        assert state.consecutive_failures == 1
        assert "disconnected" in (state.last_error or "")
        p1.read.assert_not_awaited()
        assert now[0] == state.last_finished_at  # 有始有终（running 归位）
        assert state.running is False
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# 应用层 connect 超时（外层 asyncio.wait_for 兜底）
# ---------------------------------------------------------------------------


async def test_connect_timeout_marks_failure_without_stopping_runtime() -> None:
    """connect 超过 connect_timeout：设备状态记失败（错误语义定位到 connect
    阶段），Runtime 保持 running，后续按 backoff 节流。"""
    now, clock = _fake_clock()
    hang = asyncio.Event()  # 永不 set——connect 挂起直到外层超时

    async def _hanging_connect() -> None:
        await hang.wait()

    p1 = _make_mock_protocol()
    p1.connect = AsyncMock(side_effect=_hanging_connect)
    config = _make_config()
    config.connect_timeout = 0.05  # 极短外层超时——不做真实长等待
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        config=config,
        clock=clock,
    )
    try:
        await runtime.start()
        assert runtime.running is True

        state = runtime.device_state("d1")
        assert state is not None
        assert state.connected is False
        assert state.consecutive_failures == 1
        assert "connect timeout" in (state.last_error or "")
        assert state.next_retry_at == now[0] + 1.0  # backoff 节流已就位

        # 周期 Job 已注册；节流窗口内 collect 跳过读（不形成重连风暴）
        await scheduler.trigger_job("poll:d1:default")
        assert p1.read.await_count == 0
    finally:
        await runtime.stop()


async def test_read_timeout_marks_device_disconnected_and_keeps_job() -> None:
    """外层 read_timeout 超时：acq FAILED（read timeout）、设备标记断线
    （连接级），周期 Job 保留，下次 collect 先走重连。"""
    now, clock = _fake_clock()
    hang = asyncio.Event()

    async def _hanging_read(_refs: list) -> list:
        await hang.wait()
        return []  # pragma: no cover

    p1 = _make_mock_protocol()
    p1.read = AsyncMock(side_effect=_hanging_read)
    config = _make_config()
    config.read_timeout = 0.05
    runtime, engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        config=config,
        clock=clock,
    )
    try:
        await runtime.start()
        await scheduler.trigger_job("poll:d1:default")  # 读超时

        assert "poll:d1:default" in scheduler.jobs  # Job 保留
        assert engine.points_collected == 0
        acq = runtime.acquisition_states()["poll:d1:default"]
        assert acq.consecutive_failures == 1
        assert "read timeout" in (acq.last_error or "")
        dev = runtime.device_state("d1")
        assert dev is not None
        assert dev.connected is False  # 读超时按连接级失败处理

        # 设备恢复：推进到重试窗口 → 先重连、再正常读
        p1.read.side_effect = None
        p1.read.return_value = [_value()]
        now[0] = dev.next_retry_at
        await scheduler.trigger_job("poll:d1:default")
        assert engine.points_collected == 1
        dev = runtime.device_state("d1")
        assert dev is not None and dev.connected is True
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# 热重载 × 采集状态（§10）
# ---------------------------------------------------------------------------


async def test_hot_reload_group_add_creates_state_remove_deletes() -> None:
    """group 新增 → 建立新采集状态；group 删除 → 状态随 Job 一并清理。"""
    p1 = _make_mock_protocol()
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1", polling=[PollingGroup(group="fast", interval=1.0)])},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1", group="fast")]},
    )
    try:
        await runtime.start()
        assert set(runtime.acquisition_states()) == {"poll:d1:fast"}

        # 新增 slow group
        two_groups = [
            PollingGroup(group="fast", interval=1.0),
            PollingGroup(group="slow", interval=10.0),
        ]
        new_cfg = _make_full_config(
            devices=[_make_device("d1", polling=two_groups)],
            points=[_make_point("p1", group="fast"), _make_point("p2", group="slow")],
        )
        errors = await runtime.reconfigure(new_cfg, ConfigDiff(devices=DeviceDiff(updated=["d1"])))
        assert errors == []
        assert set(runtime.acquisition_states()) == {"poll:d1:fast", "poll:d1:slow"}

        # 删除 slow group
        new_cfg2 = _make_full_config(
            devices=[_make_device("d1", polling=[PollingGroup(group="fast", interval=1.0)])],
            points=[_make_point("p1", group="fast")],
        )
        errors = await runtime.reconfigure(
            new_cfg2, ConfigDiff(devices=DeviceDiff(updated=["d1"]))
        )
        assert errors == []
        assert set(runtime.acquisition_states()) == {"poll:d1:fast"}
        assert "poll:d1:slow" not in scheduler.jobs
    finally:
        await runtime.stop()


async def test_hot_reload_interval_change_preserves_acq_state() -> None:
    """interval 变化（Job 原地替换）：采集状态对象保留——历史计数不清零。"""
    _now, clock = _fake_clock()
    p1 = _make_mock_protocol()
    p1.read.side_effect = OSError("boom")
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1", polling_interval=1.0)},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
        clock=clock,
    )
    try:
        await runtime.start()
        await runtime.engine.collect("d1", "default")  # 制造一次失败
        before = runtime.acquisition_states()["poll:d1:default"]
        assert before.consecutive_failures == 1

        new_cfg = _make_full_config(
            devices=[_make_device("d1", polling_interval=5.0)],
            points=[_make_point("p1")],
        )
        errors = await runtime.reconfigure(new_cfg, ConfigDiff(devices=DeviceDiff(updated=["d1"])))
        assert errors == []

        after = runtime.acquisition_states()["poll:d1:default"]
        assert after is before  # 同一对象——状态保留
        assert after.consecutive_failures == 1
    finally:
        await runtime.stop()


async def test_hot_reload_point_table_change_preserves_acq_state() -> None:
    """点表换绑：不触碰采集状态（点表变化与 Job 执行状态无关）。"""
    p1 = _make_mock_protocol()
    p1.read.side_effect = OSError("boom")
    runtime, _engine, _scheduler = _make_runtime(
        devices={"d1": _make_device("d1", point_table="t1")},
        protocols={"d1": p1},
        points_by_device={"d1": [_make_point("p1")]},
    )
    try:
        await runtime.start()
        await runtime.engine.collect("d1", "default")
        before = runtime.acquisition_states()["poll:d1:default"]

        new_cfg = _make_full_config(
            devices=[_make_device("d1", point_table="t2")],
            tables={"t1": [_make_point("p1")], "t2": [_make_point("p9")]},
        )
        errors = await runtime.reconfigure(new_cfg, ConfigDiff(devices=DeviceDiff(updated=["d1"])))
        assert errors == []
        assert runtime.acquisition_states()["poll:d1:default"] is before
    finally:
        await runtime.stop()


async def test_hot_reload_device_removed_cleans_device_and_acq_states() -> None:
    """设备删除：DeviceRuntimeState 与全部采集状态一并清理，Job 注销。"""
    p1 = _make_mock_protocol()
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1"), "d2": _make_device("d2")},
        protocols={"d1": p1, "d2": _make_mock_protocol()},
        points_by_device={"d1": [_make_point("p1")], "d2": [_make_point("p1")]},
    )
    try:
        await runtime.start()
        assert runtime.device_state("d1") is not None
        assert "poll:d1:default" in runtime.acquisition_states()

        new_cfg = _make_full_config(
            devices=[_make_device("d2")],
            points=[_make_point("p1")],
        )
        errors = await runtime.reconfigure(new_cfg, ConfigDiff(devices=DeviceDiff(removed=["d1"])))
        assert errors == []

        assert "poll:d1:default" not in scheduler.jobs
        assert runtime.device_state("d1") is None
        assert set(runtime.acquisition_states()) == {"poll:d2:default"}
    finally:
        await runtime.stop()


async def test_hot_reload_device_added_initializes_states() -> None:
    """设备新增：DeviceRuntimeState 与采集状态一并建立（Job 注册即建状态）。"""
    p2 = _make_mock_protocol()
    runtime, _engine, scheduler = _make_runtime(
        devices={"d1": _make_device("d1")},
        protocols={"d1": _make_mock_protocol()},
        points_by_device={"d1": [_make_point("p1")]},
        protocol_factory=lambda _cfg: p2,
    )
    try:
        await runtime.start()

        new_cfg = _make_full_config(
            devices=[_make_device("d1"), _make_device("d2")],
            points=[_make_point("p1")],
        )
        errors = await runtime.reconfigure(new_cfg, ConfigDiff(devices=DeviceDiff(added=["d2"])))
        assert errors == []

        assert "poll:d2:default" in scheduler.jobs
        assert runtime.device_state("d2") is not None
        assert "poll:d2:default" in runtime.acquisition_states()
    finally:
        await runtime.stop()
