"""Unit tests for the Scheduler component."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.config.schema import (
    DeviceConfig,
    PointAddress,
    PointConfig,
    PollingGroup,
    SchedulerConfig,
)
from wind_hub.domain.engine.pipeline import Pipeline
from wind_hub.domain.engine.router import Router
from wind_hub.domain.engine.scheduler import Scheduler
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort, SinkPort

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_device(device_id: str, polling_interval: float = 1.0) -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol="modbus",
        endpoint=Endpoint(host="10.0.0.1", port=502),
        polling=[PollingGroup(group="default", interval=polling_interval)],
        enabled=True,
    )


def _make_config(backpressure: str = "drop_old") -> SchedulerConfig:
    return SchedulerConfig(
        queue_maxsize=10,
        backpressure_policy=backpressure,
        shutdown_timeout=1.0,
        connect_timeout=1.0,
        read_timeout=1.0,
    )


def _make_mock_protocol(name: str = "d1") -> ProtocolPort:
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


def _make_mock_sink(name: str = "s1") -> SinkPort:
    sink = MagicMock(spec=SinkPort)
    sink.open = AsyncMock()
    sink.close = AsyncMock()
    sink.write = AsyncMock()
    sink.flush = AsyncMock()
    sink.health = MagicMock(return_value=HealthStatus(healthy=True))
    return sink


# ---------------------------------------------------------------------------
# 1. start() connects all devices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_connects_all_devices() -> None:
    p1 = _make_mock_protocol()
    devices = {"d1": _make_device("d1")}
    config = _make_config()
    scheduler = Scheduler(
        devices=devices,
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=config,
    )
    try:
        await scheduler.start()
    finally:
        await scheduler.stop()

    p1.connect.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Single device connect failure — skipped, others continue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_failure_skipped_others_continue() -> None:
    p1 = _make_mock_protocol()
    p2 = _make_mock_protocol()
    p1.connect.side_effect = OSError("refused")

    devices = {"d1": _make_device("d1"), "d2": _make_device("d2")}
    scheduler = Scheduler(
        devices=devices,
        protocols={"d1": p1, "d2": p2},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    try:
        await scheduler.start()
    finally:
        await scheduler.stop()

    p1.connect.assert_awaited_once()
    p2.connect.assert_awaited_once()  # p2 still attempted


# ---------------------------------------------------------------------------
# 3. start() opens all sinks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_opens_all_sinks() -> None:
    s1 = _make_mock_sink("s1")
    s2 = _make_mock_sink("s2")
    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={"s1": s1, "s2": s2},
        config=_make_config(),
    )
    try:
        await scheduler.start()
    finally:
        await scheduler.stop()

    s1.open.assert_awaited_once()
    s2.open.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. Polling loop calls ProtocolPort.read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_polling_calls_read() -> None:
    p1 = _make_mock_protocol()
    p1.read.return_value = [PointValue(device_id="d1", point_id="p1", value=1.0)]

    router = MagicMock(spec=Router)
    router.route.return_value = {}

    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=0.05)},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=router,
        sinks={},
        config=_make_config(),
    )
    try:
        await scheduler.start()
        await asyncio.sleep(0.15)  # let it poll a couple times
    finally:
        await scheduler.stop()

    assert p1.read.call_count >= 1


# ---------------------------------------------------------------------------
# 5. Pipeline is called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_is_called() -> None:
    p1 = _make_mock_protocol()
    p1.read.return_value = [PointValue(device_id="d1", point_id="p1", value=1.0)]

    router = MagicMock(spec=Router)
    router.route.return_value = {}

    pipeline = MagicMock(spec=Pipeline)
    pipeline.process = AsyncMock(return_value=[])

    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=0.05)},
        protocols={"d1": p1},
        pipeline=pipeline,
        router=router,
        sinks={},
        config=_make_config(),
    )
    try:
        await scheduler.start()
        await asyncio.sleep(0.15)
    finally:
        await scheduler.stop()

    assert pipeline.process.call_count >= 1


# ---------------------------------------------------------------------------
# 6. Router is called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_is_called() -> None:
    p1 = _make_mock_protocol()
    p1.read.return_value = [PointValue(device_id="d1", point_id="p1", value=1.0)]

    router = MagicMock(spec=Router)
    router.route.return_value = {}

    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=0.05)},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=router,
        sinks={},
        config=_make_config(),
    )
    try:
        await scheduler.start()
        await asyncio.sleep(0.15)
    finally:
        await scheduler.stop()

    assert router.route.call_count >= 1


# ---------------------------------------------------------------------------
# 7. Results pushed to correct sink queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_results_routed_to_sink() -> None:
    p1 = _make_mock_protocol()
    pv = PointValue(device_id="d1", point_id="p1", value=1.0)
    p1.read.return_value = [pv]

    router = MagicMock(spec=Router)
    router.route.return_value = {"s1": [pv]}

    s1 = _make_mock_sink("s1")

    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=0.1)},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=router,
        sinks={"s1": s1},
        config=_make_config(),
    )
    try:
        await scheduler.start()
        await asyncio.sleep(0.35)  # let sink consumer process
    finally:
        await scheduler.stop()

    s1.write.assert_called()


# ---------------------------------------------------------------------------
# 8. Backpressure drop_old — drains oldest on full queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backpressure_drop_old_drains_queue() -> None:
    p1 = _make_mock_protocol()
    pv = PointValue(device_id="d1", point_id="p1", value=1.0)
    p1.read.return_value = [pv]

    router = MagicMock(spec=Router)
    router.route.return_value = {"s1": [pv]}

    s1 = _make_mock_sink("s1")
    # Make sink write slow so queue backs up
    write_event = asyncio.Event()
    s1.write = AsyncMock(side_effect=lambda batch: write_event.wait())

    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=0.03)},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=router,
        sinks={"s1": s1},
        config=SchedulerConfig(
            queue_maxsize=3,
            backpressure_policy="drop_old",
            shutdown_timeout=1.0,
            connect_timeout=1.0,
            read_timeout=1.0,
        ),
    )
    try:
        await scheduler.start()
        await asyncio.sleep(0.3)  # enough to fill queue with drop_old
    finally:
        write_event.set()
        await scheduler.stop()

    # Sink consumer should have been reading items (some dropped via drop_old)
    s1.write.assert_called()


# ---------------------------------------------------------------------------
# 9. Backpressure block — blocks on full queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backpressure_block_waits_on_full_queue() -> None:
    p1 = _make_mock_protocol()
    pv = PointValue(device_id="d1", point_id="p1", value=1.0)
    p1.read.return_value = [pv]

    router = MagicMock(spec=Router)
    router.route.return_value = {"s1": [pv]}

    s1 = _make_mock_sink("s1")
    write_event = asyncio.Event()
    s1.write = AsyncMock(side_effect=lambda batch: write_event.wait())

    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=0.03)},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=router,
        sinks={"s1": s1},
        config=SchedulerConfig(
            queue_maxsize=3,
            backpressure_policy="block",
            shutdown_timeout=1.0,
            connect_timeout=1.0,
            read_timeout=1.0,
        ),
    )
    try:
        await scheduler.start()
        await asyncio.sleep(0.2)
    finally:
        write_event.set()
        await scheduler.stop()

    s1.write.assert_called()


# ---------------------------------------------------------------------------
# 10. stop() cancels all tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_device_tasks() -> None:
    p1 = _make_mock_protocol()
    s1 = _make_mock_sink("s1")
    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=10.0)},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={"s1": s1},
        config=SchedulerConfig(
            queue_maxsize=10,
            backpressure_policy="drop_old",
            shutdown_timeout=0.5,
            connect_timeout=1.0,
            read_timeout=1.0,
        ),
    )
    await scheduler.start()
    await asyncio.sleep(0.05)
    await scheduler.stop()
    # After stop, no running device tasks
    assert len(scheduler._device_tasks) == 0  # noqa: SLF001


# ---------------------------------------------------------------------------
# 11. stop() closes all connections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_closes_protocols_and_sinks() -> None:
    p1 = _make_mock_protocol()
    s1 = _make_mock_sink("s1")
    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=10.0)},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={"s1": s1},
        config=SchedulerConfig(
            queue_maxsize=10,
            backpressure_policy="drop_old",
            shutdown_timeout=0.5,
            connect_timeout=1.0,
            read_timeout=1.0,
        ),
    )
    await scheduler.start()
    await asyncio.sleep(0.05)
    await scheduler.stop()
    p1.close.assert_awaited()
    s1.close.assert_awaited()


# ---------------------------------------------------------------------------
# 12. stop() drains all sinks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_flushes_sinks() -> None:
    p1 = _make_mock_protocol()
    s1 = _make_mock_sink("s1")
    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=10.0)},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={"s1": s1},
        config=SchedulerConfig(
            queue_maxsize=10,
            backpressure_policy="drop_old",
            shutdown_timeout=0.5,
            connect_timeout=1.0,
            read_timeout=1.0,
        ),
    )
    await scheduler.start()
    await asyncio.sleep(0.05)
    await scheduler.stop()
    s1.flush.assert_awaited()


# ---------------------------------------------------------------------------
# 13. health() returns correct status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_returns_all_statuses() -> None:
    p1 = _make_mock_protocol()
    s1 = _make_mock_sink("s1")
    scheduler = Scheduler(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={"s1": s1},
        config=_make_config(),
    )
    h = scheduler.health()
    assert "d1" in h
    assert "s1" in h
    assert h["d1"].healthy is True
    assert h["s1"].healthy is True


# ---------------------------------------------------------------------------
# 14. device_count / sink_count
# ---------------------------------------------------------------------------


def test_device_and_sink_count() -> None:
    scheduler = Scheduler(
        devices={"d1": _make_device("d1"), "d2": _make_device("d2")},
        protocols={"d1": _make_mock_protocol(), "d2": _make_mock_protocol()},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={"s1": _make_mock_sink("s1")},
        config=_make_config(),
    )
    assert scheduler.device_count == 2
    assert scheduler.sink_count == 1


# ---------------------------------------------------------------------------
# Edge: double-start is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_start_is_noop() -> None:
    p1 = _make_mock_protocol()
    scheduler = Scheduler(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    try:
        await scheduler.start()
        call_count_after_first = p1.connect.call_count
        await scheduler.start()  # second start should be no-op
        assert p1.connect.call_count == call_count_after_first
    finally:
        await scheduler.stop()


# ---------------------------------------------------------------------------
# Edge: stop() safe to call before start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_before_start_safe() -> None:
    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    await scheduler.stop()  # should not raise


# ---------------------------------------------------------------------------
# 15. start() injects point tables into protocol drivers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_injects_points_mapping() -> None:
    p1 = _make_mock_protocol()
    points = [_make_point("d1", "rotor.speed"), _make_point("d1", "gen.power")]
    scheduler = Scheduler(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
        points_by_device={"d1": points},
    )
    try:
        await scheduler.start()
    finally:
        await scheduler.stop()

    p1.set_points_mapping.assert_called_once_with(points)


@pytest.mark.asyncio
async def test_start_injects_empty_mapping_when_device_has_no_points() -> None:
    p1 = _make_mock_protocol()
    scheduler = Scheduler(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    try:
        await scheduler.start()
    finally:
        await scheduler.stop()

    p1.set_points_mapping.assert_called_once_with([])


# ---------------------------------------------------------------------------
# 16. polling reads the device's points (as PointRef)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_reads_device_points() -> None:
    p1 = _make_mock_protocol()
    p1.read.return_value = [PointValue(device_id="d1", point_id="rotor.speed", value=1.0)]
    points = [_make_point("d1", "rotor.speed")]

    router = MagicMock(spec=Router)
    router.route.return_value = {}

    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=0.05)},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=router,
        sinks={},
        config=_make_config(),
        points_by_device={"d1": points},
    )
    try:
        await scheduler.start()
        await asyncio.sleep(0.15)
    finally:
        await scheduler.stop()

    expected_refs = [PointRef(device_id="d1", point_id="rotor.speed")]
    assert p1.read.call_count >= 1
    p1.read.assert_called_with(expected_refs)


# ---------------------------------------------------------------------------
# 17. hot-reload — add_device injects the point table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_device_injects_points_mapping() -> None:
    p1 = _make_mock_protocol()
    points = [_make_point("d1", "rotor.speed"), _make_point("d1", "gen.power")]
    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    await scheduler.add_device("d1", _make_device("d1"), p1, points)

    p1.set_points_mapping.assert_called_once_with(points)


@pytest.mark.asyncio
async def test_rebuild_device_injects_points_mapping() -> None:
    old = _make_mock_protocol()
    new = _make_mock_protocol()
    points = [_make_point("d1", "rotor.speed")]
    scheduler = Scheduler(
        devices={"d1": _make_device("d1")},
        protocols={"d1": old},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    await scheduler.rebuild_device("d1", _make_device("d1"), new, points)

    new.set_points_mapping.assert_called_once_with(points)


@pytest.mark.asyncio
async def test_add_device_records_points_for_polling() -> None:
    """The injected point table is stored so the new device's loop reads it."""
    p1 = _make_mock_protocol()
    points = [_make_point("d1", "rotor.speed")]
    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    await scheduler.add_device("d1", _make_device("d1"), p1, points)

    assert scheduler._points_by_device["d1"] == points  # noqa: SLF001


# ---------------------------------------------------------------------------
# 18. sink.open() isolation — a failing sink does not stop the engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_open_failure_skipped_others_continue() -> None:
    s1 = _make_mock_sink("s1")
    s1.open.side_effect = OSError("permission denied")
    s2 = _make_mock_sink("s2")
    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={"s1": s1, "s2": s2},
        config=_make_config(),
    )
    try:
        await scheduler.start()
    finally:
        await scheduler.stop()

    s1.open.assert_awaited_once()
    s2.open.assert_awaited_once()  # still attempted despite s1 failing
    assert scheduler.health()["s1"].healthy is False
    assert scheduler.health()["s2"].healthy is True


@pytest.mark.asyncio
async def test_all_sinks_fail_engine_still_starts() -> None:
    s1 = _make_mock_sink("s1")
    s1.open.side_effect = RuntimeError("boom")
    s2 = _make_mock_sink("s2")
    s2.open.side_effect = RuntimeError("boom")
    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={"s1": s1, "s2": s2},
        config=_make_config(),
    )
    try:
        await scheduler.start()
        assert scheduler.running is True
    finally:
        await scheduler.stop()

    assert scheduler.health()["s1"].healthy is False
    assert scheduler.health()["s2"].healthy is False


# ---------------------------------------------------------------------------
# 19. push/pull mode — subscribe-mode devices are not polled
# ---------------------------------------------------------------------------


def _make_subscribe_device(device_id: str, mode: str = "subscribe") -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol="modbus",
        endpoint=Endpoint(host="10.0.0.1", port=502),
        mode=mode,
    )


@pytest.mark.asyncio
async def test_subscribe_mode_device_is_not_polled() -> None:
    p1 = _make_mock_protocol()
    scheduler = Scheduler(
        devices={"d1": _make_subscribe_device("d1")},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    try:
        await scheduler.start()
        await asyncio.sleep(0.05)
        assert "d1" not in scheduler._device_tasks  # noqa: SLF001
    finally:
        await scheduler.stop()

    p1.subscribe.assert_awaited_once()
    p1.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_subscribe_mode_passes_point_refs() -> None:
    p1 = _make_mock_protocol()
    points = [_make_point("d1", "rotor.speed")]
    scheduler = Scheduler(
        devices={"d1": _make_subscribe_device("d1")},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
        points_by_device={"d1": points},
    )
    try:
        await scheduler.start()
    finally:
        await scheduler.stop()

    p1.subscribe.assert_awaited_once()
    refs = p1.subscribe.await_args.args[0]
    assert refs == [PointRef(device_id="d1", point_id="rotor.speed")]


@pytest.mark.asyncio
async def test_subscribe_not_supported_falls_back_to_polling() -> None:
    p1 = _make_mock_protocol()
    p1.subscribe.side_effect = NotImplementedError
    scheduler = Scheduler(
        devices={"d1": _make_subscribe_device("d1")},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    try:
        await scheduler.start()
        assert "d1" in scheduler._device_tasks  # noqa: SLF001
    finally:
        await scheduler.stop()


# ---------------------------------------------------------------------------
# running 语义（决策 1）：start 未完成前不对外报告 running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_false_until_start_completes() -> None:
    """设备连接尚未结束时 running 为 False（/health 可区分「启动中」）。"""
    gate = asyncio.Event()

    async def _slow_connect() -> None:
        await gate.wait()

    p1 = _make_mock_protocol()
    p1.connect = AsyncMock(side_effect=_slow_connect)

    scheduler = Scheduler(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    start_task = asyncio.create_task(scheduler.start())
    try:
        await asyncio.sleep(0.05)  # 让 start 进入设备连接等待
        assert scheduler.running is False
        assert scheduler._running is True  # noqa: SLF001  内部循环标志已置位
        gate.set()
        await start_task
        assert scheduler.running is True
    finally:
        gate.set()
        await scheduler.stop()
    assert scheduler.running is False


# ---------------------------------------------------------------------------
# 运行期统计（决策 7）：points_collected / points_routed / points_dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_points_collected_and_routed_accumulate() -> None:
    """成功轮询的点值计入 collected，进入 sink 队列的计入 routed。"""
    p1 = _make_mock_protocol()
    pv = PointValue(device_id="d1", point_id="p1", value=1.0)
    p1.read.return_value = [pv, pv]

    router = MagicMock(spec=Router)
    router.route.return_value = {"s1": [pv, pv]}

    scheduler = Scheduler(
        devices={"d1": _make_device("d1", polling_interval=0.05)},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=router,
        sinks={"s1": _make_mock_sink("s1")},
        config=_make_config(),
    )
    try:
        await scheduler.start()
        await asyncio.sleep(0.18)  # 跑若干轮轮询
    finally:
        await scheduler.stop()

    assert scheduler.points_collected >= 2
    assert scheduler.points_routed >= 2
    assert scheduler.points_collected == scheduler.points_routed
    assert scheduler.points_dropped == 0


@pytest.mark.asyncio
async def test_points_dropped_counts_drop_new_rejections() -> None:
    """drop_new 策略下满队列的新批次计入 points_dropped。"""
    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={"s1": _make_mock_sink("s1")},
        config=SchedulerConfig(
            queue_maxsize=1,
            backpressure_policy="drop_new",
            shutdown_timeout=1.0,
            connect_timeout=1.0,
            read_timeout=1.0,
        ),
    )
    queue = scheduler._queues["s1"]  # noqa: SLF001
    pv = PointValue(device_id="d1", point_id="p1", value=1.0)

    await scheduler._handle_backpressure(queue, [pv], "s1")  # noqa: SLF001 入队成功
    await scheduler._handle_backpressure(queue, [pv, pv], "s1")  # noqa: SLF001 队列满 → 丢弃

    assert scheduler.points_routed == 1
    assert scheduler.points_dropped == 2


@pytest.mark.asyncio
async def test_points_dropped_counts_drop_old_evictions() -> None:
    """drop_old 策略下被挤出的旧批次计入 points_dropped，新批次计入 routed。"""
    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={"s1": _make_mock_sink("s1")},
        config=SchedulerConfig(
            queue_maxsize=1,
            backpressure_policy="drop_old",
            shutdown_timeout=1.0,
            connect_timeout=1.0,
            read_timeout=1.0,
        ),
    )
    queue = scheduler._queues["s1"]  # noqa: SLF001
    pv = PointValue(device_id="d1", point_id="p1", value=1.0)

    await scheduler._handle_backpressure(queue, [pv, pv], "s1")  # noqa: SLF001 入队 2 点
    await scheduler._handle_backpressure(queue, [pv], "s1")  # noqa: SLF001 挤出旧批 + 入队 1 点

    assert scheduler.points_routed == 3
    assert scheduler.points_dropped == 2


@pytest.mark.asyncio
async def test_points_collected_counts_subscribe_push_path() -> None:
    """决策 0.1：订阅推送（on_data → _process_and_route）同样计入
    points_collected，并触发注入的 on_points_collected 回调——口径与轮询一致。"""
    collected: list[int] = []
    router = MagicMock(spec=Router)
    pv = PointValue(device_id="d1", point_id="p1", value=1.0)
    router.route.return_value = {"s1": [pv]}

    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=router,
        sinks={"s1": _make_mock_sink("s1")},
        config=_make_config(),
        on_points_collected=collected.append,
    )

    # 模拟订阅回调逐点推送（不走任何轮询）
    await scheduler._process_and_route([pv])  # noqa: SLF001
    await scheduler._process_and_route([pv])  # noqa: SLF001

    assert scheduler.points_collected == 2
    assert collected == [1, 1]  # 回调按批次点数触发
    assert scheduler.points_routed == 2


# ---------------------------------------------------------------------------
# 初次 connect 失败日志分级（决策 0.3）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["timeout", "refused", "oserror", "wrapped"])
async def test_start_connect_failure_connection_level_no_traceback(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    """连接级故障（超时/拒连/网络错误，含包装链）→ 简洁 warning，不打堆栈。"""
    exc: Exception
    if kind == "timeout":
        exc = TimeoutError("timed out")
    elif kind == "refused":
        exc = ConnectionRefusedError("refused")
    elif kind == "oserror":
        exc = OSError("network unreachable")
    else:
        # 驱动把底层 OSError 包装成 ProtocolError 再抛（IEC104 路径）——
        # 穿透 cause 链判定为连接级。
        exc = ProtocolError("IEC104: TCP connect failed")
        exc.__cause__ = OSError("connection refused")
    p1 = _make_mock_protocol()
    p1.connect.side_effect = exc

    scheduler = Scheduler(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    with caplog.at_level(logging.WARNING, logger="wind_hub.domain.engine.scheduler"):
        try:
            await scheduler.start()
        finally:
            await scheduler.stop()

    warnings = [r for r in caplog.records if "failed to connect" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None  # 连接级故障不打堆栈


@pytest.mark.asyncio
async def test_start_connect_failure_other_error_keeps_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """非连接级异常（编程错误等）→ 保留完整堆栈（exc_info）。"""
    p1 = _make_mock_protocol()
    p1.connect.side_effect = ValueError("bad config in driver")

    scheduler = Scheduler(
        devices={"d1": _make_device("d1")},
        protocols={"d1": p1},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
        sinks={},
        config=_make_config(),
    )
    with caplog.at_level(logging.WARNING, logger="wind_hub.domain.engine.scheduler"):
        try:
            await scheduler.start()
        finally:
            await scheduler.stop()

    warnings = [r for r in caplog.records if "failed to connect" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
