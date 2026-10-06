"""SinkRuntime ownership、生命周期与资源不变量的直接验证。

使用真实 SinkRuntime + mock SinkPort；覆盖：

- ownership：Sink 注册表 / queue / 消费者任务 / unhealthy 状态只归
  SinkRuntime；CollectorRuntime 不再持有重复簿记，且引擎的
  SinkDispatchPort 直接绑定 SinkRuntime；
- 生命周期：start 建消费者、stop 排空并取消消费者、重复 start/stop 幂等；
- 热更新：add/remove 无消费者与 queue 泄漏，rebuild 旧实例只 close 一次、
  消费者不重复，exclusive-open 的 close-first 顺序与 open 失败回滚；
- 背压：drop_old / drop_new / block 三种策略语义；
- 失败隔离：单 Sink write 失败不拖垮消费者与其他 Sink；
- 停机：队列中 pending 数据在 close 前写完，无孤儿 asyncio task。

端到端 fan-out、reconfigure 协调与聚合 health 仍由 test_runtime.py 覆盖。
"""

from __future__ import annotations  # noqa: I001

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.runtime import CollectorRuntime
from wind_hub_collector.application.runtime.sink_runtime import SinkRuntime
from wind_hub_core.config import ResolvedSinkConfig, RuntimeConfig
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runtime_config(backpressure: str = "drop_old", queue_maxsize: int = 10) -> RuntimeConfig:
    return RuntimeConfig(
        queue_maxsize=queue_maxsize,
        backpressure_policy=backpressure,
        shutdown_timeout=0.5,
        connect_timeout=0.2,
        read_timeout=0.2,
    )


def _mock_sink() -> SinkPort:
    sink = MagicMock(spec=SinkPort)
    sink.open = AsyncMock()
    sink.close = AsyncMock()
    sink.write = AsyncMock()
    sink.flush = AsyncMock()
    sink.health = MagicMock(return_value=HealthStatus(healthy=True))
    return sink


def _value(device_id: str = "d1", point_id: str = "p1") -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=1.0)


def _sink_runtime(
    sink_names: tuple[str, ...] = ("s1",),
    backpressure: str = "drop_old",
    queue_maxsize: int = 10,
    sink_factory=None,
) -> tuple[SinkRuntime, dict[str, SinkPort]]:
    sinks = {name: _mock_sink() for name in sink_names}
    return (
        SinkRuntime(
            sinks,
            _runtime_config(backpressure, queue_maxsize),
            sink_factory=sink_factory,
        ),
        sinks,
    )


def _sink_cfg(name: str) -> ResolvedSinkConfig:
    return ResolvedSinkConfig(
        name=name,
        type="file",
        connection={"path": f"/tmp/{name}.jsonl"},
    )


class _FakeEngine:
    """``AcquisitionEngine`` 的内存替身——只记录 CollectorRuntime 的装配缝绑定。"""

    def __init__(self) -> None:
        self.sink_dispatch: object | None = None

    def attach_sink_dispatch(self, dispatch: object) -> None:
        self.sink_dispatch = dispatch

    def attach_device_state(self, device_state: object) -> None:
        pass

    def attach_acquisition_state(self, acquisition_state: object) -> None:
        pass

    @property
    def points_collected(self) -> int:
        return 0

    async def collect(self, *args: object) -> None:
        pass

    async def process(self, *args: object) -> None:
        pass


class _ExclusiveSink:
    def __init__(self, events: list[str], name: str, fail_open: bool = False) -> None:
        self.events = events
        self.name = name
        self.fail_open = fail_open

    @property
    def exclusive_open(self) -> bool:
        return True

    async def open(self) -> None:
        self.events.append(f"{self.name}:open")
        if self.fail_open:
            raise OSError("bind failed")

    async def close(self) -> None:
        self.events.append(f"{self.name}:close")

    async def write(self, batch: list[PointValue]) -> None:
        del batch

    async def flush(self) -> None:
        self.events.append(f"{self.name}:flush")

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True)


async def _wait_for(cond, timeout: float = 2.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError(f"timeout waiting for {what}")
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


class TestOwnership:
    def test_sink_state_lives_only_in_sink_runtime(self) -> None:
        """Sink 注册表/queue/消费者/unhealthy 只在 SinkRuntime；CollectorRuntime 无重复簿记。"""
        engine = _FakeEngine()
        rt = CollectorRuntime(
            devices={},
            sinks={"s1": _mock_sink()},
            engine=engine,  # type: ignore[arg-type]  # 鸭子类型替身，仅实现装配缝
            config=_runtime_config(),
            tasks={},
        )

        assert rt.sink_runtime.sinks.keys() == {"s1"}
        assert rt.sink_runtime.queue_depths() == {"s1": 0}
        for legacy in ("_sinks", "_queues", "_sink_tasks", "_unhealthy_sinks", "_sink_dispatcher"):
            assert not hasattr(rt, legacy), f"CollectorRuntime 仍持有 {legacy}"

    def test_engine_dispatch_port_bound_to_sink_runtime(self) -> None:
        """SinkDispatchPort 的直接实现者是 SinkRuntime，不是 CollectorRuntime。"""
        engine = _FakeEngine()
        rt = CollectorRuntime(
            devices={},
            sinks={"s1": _mock_sink()},
            engine=engine,  # type: ignore[arg-type]
            config=_runtime_config(),
            tasks={},
        )

        assert engine.sink_dispatch is rt.sink_runtime
        assert not hasattr(rt, "dispatch")

    def test_collector_runtime_facades_delegate_to_sink_runtime(self) -> None:
        """sinks / sink_count / 队列深度 / 派发计数为只读透传。"""
        engine = _FakeEngine()
        rt = CollectorRuntime(
            devices={},
            sinks={"s1": _mock_sink()},
            engine=engine,  # type: ignore[arg-type]
            config=_runtime_config(),
            tasks={},
        )

        assert rt.sinks == rt.sink_runtime.sinks
        # 注册表对外只读——变更必须经 owner 的行为接口，不能绕过。
        with pytest.raises(TypeError):
            rt.sinks["s2"] = _mock_sink()  # type: ignore[index]
        assert rt.sink_count == 1
        assert rt.sink_queue_depths() == rt.sink_runtime.queue_depths()
        assert rt.points_routed == rt.sink_runtime.points_routed
        assert rt.points_dropped == rt.sink_runtime.points_dropped


# ---------------------------------------------------------------------------
# 启动 / 停止
# ---------------------------------------------------------------------------


class TestStartStop:
    async def test_start_opens_sinks_and_creates_consumers(self) -> None:
        sr, sinks = _sink_runtime(("s1", "s2"))
        await sr.start()
        try:
            sinks["s1"].open.assert_awaited_once()
            sinks["s2"].open.assert_awaited_once()
            assert set(sr._consumer_tasks) == {"s1", "s2"}  # noqa: SLF001
            assert all(not task.done() for task in sr._consumer_tasks.values())  # noqa: SLF001
        finally:
            await sr.stop()

    async def test_stop_drains_cancels_and_closes(self) -> None:
        sr, sinks = _sink_runtime(("s1",))
        await sr.start()
        consumer = sr._consumer_tasks["s1"]  # noqa: SLF001

        await sr.stop()

        assert consumer.done()
        assert sr._consumer_tasks == {}  # noqa: SLF001
        sinks["s1"].flush.assert_awaited_once()
        sinks["s1"].close.assert_awaited_once()

    async def test_stop_flushes_after_pending_writes_complete(self) -> None:
        """停机语义：队列中已入队的数据在 flush/close 之前被消费者写完。"""
        events: list[str] = []
        gate = asyncio.Event()
        sink = _mock_sink()

        async def gated_write(batch: list[PointValue]) -> None:
            await gate.wait()
            events.append(f"write:{len(batch)}")

        async def record_flush() -> None:
            events.append("flush")

        async def record_close() -> None:
            events.append("close")

        sink.write = AsyncMock(side_effect=gated_write)
        sink.flush = AsyncMock(side_effect=record_flush)
        sink.close = AsyncMock(side_effect=record_close)

        sr = SinkRuntime({"s1": sink}, _runtime_config())
        await sr.start()
        # 消费者取走第 1 批并阻塞在 write；第 2 批留在队列中（pending）。
        await sr.dispatch({"s1": [_value(point_id="p1")]})
        await _wait_for(lambda: sink.write.await_count == 1, what="consumer entered write")
        await sr.dispatch({"s1": [_value(point_id="p2"), _value(point_id="p3")]})

        gate.set()
        await sr.stop()

        assert events == ["write:1", "write:2", "flush", "close"]
        assert sr.queue_depths() == {"s1": 0}

    async def test_repeated_start_stop_idempotent(self) -> None:
        sr, sinks = _sink_runtime(("s1",))
        for _ in range(2):
            await sr.start()
            assert set(sr._consumer_tasks) == {"s1"}  # noqa: SLF001
            await sr.stop()
            assert sr._consumer_tasks == {}  # noqa: SLF001

        assert sinks["s1"].open.await_count == 2
        assert sinks["s1"].flush.await_count == 2
        assert sinks["s1"].close.await_count == 2

    async def test_open_failure_marks_unhealthy_and_skips_consumer(self) -> None:
        sr, sinks = _sink_runtime(("s1", "s2"))
        sinks["s1"].open = AsyncMock(side_effect=RuntimeError("open boom"))
        await sr.start()
        try:
            assert set(sr._consumer_tasks) == {"s2"}  # noqa: SLF001
            health = sr.health()
            assert health["s1"].healthy is False
            assert health["s1"].message == "open failed"
            assert health["s2"].healthy is True
        finally:
            await sr.stop()


# ---------------------------------------------------------------------------
# 热更新——资源不变量
# ---------------------------------------------------------------------------


class TestHotUpdate:
    async def test_add_sink_creates_consumer_without_leak(self) -> None:
        sr, _ = _sink_runtime(("s1",))
        await sr.start()
        try:
            created = _mock_sink()
            await sr.add_sink("s2", _sink_cfg("s2"), created)

            created.open.assert_awaited_once()
            assert sr.sinks["s2"] is created
            assert set(sr._consumer_tasks) == {"s1", "s2"}  # noqa: SLF001
            assert sr.queue_depths() == {"s1": 0, "s2": 0}
        finally:
            await sr.stop()

    async def test_remove_sink_releases_consumer_queue_and_state(self) -> None:
        sr, _ = _sink_runtime(("s1", "s2"))
        await sr.start()
        try:
            removed_consumer = sr._consumer_tasks["s2"]  # noqa: SLF001
            await sr.remove_sink("s2")

            assert removed_consumer.done()
            assert "s2" not in sr.sinks
            assert set(sr._consumer_tasks) == {"s1"}  # noqa: SLF001
            assert sr.queue_depths() == {"s1": 0}
            assert not sr._consumer_tasks["s1"].done()  # noqa: SLF001
        finally:
            await sr.stop()

    async def test_rebuild_closes_old_once_and_keeps_single_consumer(self) -> None:
        sr, sinks = _sink_runtime(("s1",))
        old_sink = sinks["s1"]
        await sr.start()
        try:
            old_consumer = sr._consumer_tasks["s1"]  # noqa: SLF001
            new_sink = _mock_sink()
            await sr.rebuild_sink("s1", _sink_cfg("s1"), new_sink)

            old_sink.flush.assert_awaited_once()
            old_sink.close.assert_awaited_once()
            new_sink.open.assert_awaited_once()
            assert sr.sinks["s1"] is new_sink
            assert old_consumer.done()
            new_consumer = sr._consumer_tasks["s1"]  # noqa: SLF001
            assert new_consumer is not old_consumer
            assert not new_consumer.done()
            assert len(sr._consumer_tasks) == 1  # noqa: SLF001
        finally:
            await sr.stop()

    async def test_add_existing_sink_converges_via_rebuild(self) -> None:
        """重复热增同名 sink：按 rebuild 收敛，不产生第二个消费者。"""
        sr, sinks = _sink_runtime(("s1",))
        old_sink = sinks["s1"]
        await sr.start()
        try:
            replacement = _mock_sink()
            await sr.add_sink("s1", _sink_cfg("s1"), replacement)

            assert sr.sinks["s1"] is replacement
            old_sink.close.assert_awaited_once()
            assert len(sr._consumer_tasks) == 1  # noqa: SLF001
        finally:
            await sr.stop()

    async def test_rebuild_before_start_does_not_create_consumer(self) -> None:
        """未启动时重建只换注册表——消费者由后续 start 统一创建。"""
        sr, _ = _sink_runtime(("s1",))
        new_sink = _mock_sink()
        await sr.rebuild_sink("s1", _sink_cfg("s1"), new_sink)

        assert sr.sinks["s1"] is new_sink
        assert sr._consumer_tasks == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# Exclusive-open
# ---------------------------------------------------------------------------


class TestExclusiveOpen:
    async def test_exclusive_sink_closes_old_before_opening_new(self) -> None:
        events: list[str] = []
        old_sink = _ExclusiveSink(events, "old")
        new_sink = _ExclusiveSink(events, "new")
        sr = SinkRuntime({"s1": old_sink}, _runtime_config())

        await sr.rebuild_sink("s1", _sink_cfg("s1"), new_sink)

        assert events[:3] == ["old:flush", "old:close", "new:open"]
        assert sr.sinks["s1"] is new_sink

    async def test_exclusive_sink_open_failure_restores_old_instance(self) -> None:
        events: list[str] = []
        old_sink = _ExclusiveSink(events, "old")
        new_sink = _ExclusiveSink(events, "new", fail_open=True)
        sr = SinkRuntime({"s1": old_sink}, _runtime_config())

        with pytest.raises(OSError, match="bind failed"):
            await sr.rebuild_sink("s1", _sink_cfg("s1"), new_sink)

        assert events == ["old:flush", "old:close", "new:open", "old:open"]
        assert sr.sinks["s1"] is old_sink

    async def test_exclusive_open_failure_while_running_restarts_old_consumer(self) -> None:
        """运行中重建失败回滚旧实例后，旧实例的消费者必须恢复且不重复。"""
        events: list[str] = []
        old_sink = _ExclusiveSink(events, "old")
        new_sink = _ExclusiveSink(events, "new", fail_open=True)
        sr = SinkRuntime({"s1": old_sink}, _runtime_config())
        await sr.start()
        try:
            old_consumer = sr._consumer_tasks["s1"]  # noqa: SLF001
            with pytest.raises(OSError, match="bind failed"):
                await sr.rebuild_sink("s1", _sink_cfg("s1"), new_sink)

            assert old_consumer.done()
            restored = sr._consumer_tasks["s1"]  # noqa: SLF001
            assert restored is not old_consumer
            assert not restored.done()
            assert len(sr._consumer_tasks) == 1  # noqa: SLF001
            assert "open failed" not in (sr.health()["s1"].message or "")
        finally:
            await sr.stop()


# ---------------------------------------------------------------------------
# 背压与派发
# ---------------------------------------------------------------------------


class TestBackpressure:
    async def test_dispatch_to_unknown_sink_skipped(self) -> None:
        sr, _ = _sink_runtime()
        await sr.dispatch({"ghost": [_value()]})
        assert sr.points_routed == 0
        assert sr.points_dropped == 0

    async def test_backpressure_drop_old_evicts_oldest(self) -> None:
        sr, _ = _sink_runtime(backpressure="drop_old", queue_maxsize=1)
        await sr.dispatch({"s1": [_value(point_id="p1")]})
        await sr.dispatch({"s1": [_value(point_id="p2"), _value(point_id="p3")]})
        assert sr.points_dropped == 1  # 最旧批次被驱逐
        assert sr.points_routed == 3
        assert sr.queue_depths() == {"s1": 1}

    async def test_backpressure_drop_new_discards_incoming(self) -> None:
        sr, _ = _sink_runtime(backpressure="drop_new", queue_maxsize=1)
        await sr.dispatch({"s1": [_value(point_id="p1")]})
        await sr.dispatch({"s1": [_value(point_id="p2"), _value(point_id="p3")]})
        assert sr.points_dropped == 2  # 新批次整体丢弃
        assert sr.points_routed == 1
        assert sr.queue_depths() == {"s1": 1}

    async def test_backpressure_block_waits_for_queue_space(self) -> None:
        """block：queue 满时 producer 挂起，直到有空间后完成入队。"""
        sr, _ = _sink_runtime(backpressure="block", queue_maxsize=1)
        await sr.dispatch({"s1": [_value(point_id="p1")]})

        blocked = asyncio.create_task(sr.dispatch({"s1": [_value(point_id="p2")]}))
        await asyncio.sleep(0.05)
        assert not blocked.done(), "block 策略下满队列必须挂起 producer"
        assert sr.points_routed == 1

        sr._queues["s1"].get_nowait()  # noqa: SLF001  # 模拟消费者腾出空间
        await asyncio.wait_for(blocked, timeout=1.0)
        assert sr.points_routed == 2
        assert sr.queue_depths() == {"s1": 1}

    async def test_empty_batch_ignored(self) -> None:
        sr, _ = _sink_runtime()
        await sr.dispatch({"s1": []})
        assert sr.points_routed == 0
        assert sr.queue_depths() == {"s1": 0}


# ---------------------------------------------------------------------------
# 失败隔离
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    async def test_write_failure_keeps_consumer_and_other_sinks_alive(self) -> None:
        """单 Sink 单次 write 失败：记日志后继续消费，不标记 unhealthy，不影响其他 Sink。"""
        sr, sinks = _sink_runtime(("s1", "s2"))
        sinks["s1"].write = AsyncMock(side_effect=[RuntimeError("boom"), None, None])
        await sr.start()
        try:
            await sr.dispatch({"s1": [_value(point_id="p1")], "s2": [_value(point_id="p1")]})
            await sr.dispatch({"s1": [_value(point_id="p2")]})
            await _wait_for(lambda: sinks["s1"].write.await_count == 2, what="s1 recovered")
            await _wait_for(lambda: sinks["s2"].write.await_count == 1, what="s2 delivered")

            assert not sr._consumer_tasks["s1"].done()  # noqa: SLF001
            assert all(h.healthy for h in sr.health().values())
            # s1 失败的批次被丢弃（不重投），后续批次正常消费
            second_batch = sinks["s1"].write.await_args_list[1][0][0]
            assert [v.point_id for v in second_batch] == ["p2"]
        finally:
            await sr.stop()

    async def test_stop_leaves_no_orphan_consumer_tasks(self) -> None:
        sr, _ = _sink_runtime(("s1", "s2"))
        await sr.start()
        consumers = list(sr._consumer_tasks.values())  # noqa: SLF001

        await sr.stop()
        await asyncio.sleep(0)

        assert all(task.done() for task in consumers)
        assert sr._consumer_tasks == {}  # noqa: SLF001
