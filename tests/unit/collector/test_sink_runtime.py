"""新 Collector SinkRuntime 队列/背压/消费者/热重建单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from collector.application.config import RuntimeParams
from collector.application.reload import SinkDiff
from collector.application.sink_runtime import SinkRuntime
from collector.domain.acquisition import SinkDelivery
from core.domain import Sink
from core.domain.point_value import PointValue
from tests.support.new_collector import FakeSink


def _value(n: float = 1.0) -> PointValue:
    return PointValue(device_id="d", point_id="p", value=n)


def _delivery(*values: PointValue, task_id: str = "t1") -> SinkDelivery:
    return SinkDelivery(task_id=task_id, values=list(values))


def _runtime(
    sinks: dict[str, FakeSink] | None = None,
    *,
    policy: str = "drop_old",
    maxsize: int = 2,
    factory=None,
) -> SinkRuntime:
    return SinkRuntime(
        dict(sinks or {}),
        RuntimeParams(queue_maxsize=maxsize, backpressure_policy=policy, shutdown_timeout=0.5),  # type: ignore[arg-type]
        factory,
    )


async def test_start_opens_sinks_and_consumes_dispatches():
    sink = FakeSink()
    runtime = _runtime({"s1": sink})
    await runtime.start()
    assert sink.open_calls == 1

    await runtime.dispatch({"s1": _delivery(_value())})
    await asyncio.sleep(0.05)
    assert len(sink.batches) == 1
    assert runtime.points_routed == 1

    await runtime.stop()
    assert sink.flush_calls == 1 and sink.close_calls == 1


async def test_open_failure_marks_unhealthy_and_skips_consumer():
    bad = FakeSink()
    bad.fail_open = True
    good = FakeSink()
    runtime = _runtime({"bad": bad, "good": good})
    await runtime.start()
    health = await runtime.health()
    assert not health["bad"].healthy
    assert health["good"].healthy

    await runtime.dispatch({"good": _delivery(_value())})
    await asyncio.sleep(0.05)
    assert len(good.batches) == 1
    await runtime.stop()


async def test_failed_open_recovers_and_restarts_consumer():
    sink = FakeSink()
    sink.fail_open = True
    runtime = _runtime({"s1": sink})
    await runtime.start()
    assert "s1" in runtime._unhealthy
    assert not (await runtime.health())["s1"].healthy

    sink.fail_open = False
    await runtime._watchdogs["s1"].check_once()
    assert "s1" not in runtime._unhealthy
    # 后台 watchdog 周期与手动 check_once 可能各自触发一次恢复，
    # 恢复语义是幂等的；只断言确实重新打开过，不锁定具体次数。
    assert sink.open_calls >= 2

    await runtime.dispatch({"s1": _delivery(_value())})
    await asyncio.sleep(0.05)
    assert len(sink.batches) == 1
    await runtime.stop()


async def test_drop_new_discards_when_full():
    sink = FakeSink()
    runtime = _runtime({"s1": sink}, policy="drop_new", maxsize=1)
    # 不 start → 无消费者 → 队列堆积
    await runtime.dispatch({"s1": _delivery(_value(1.0))})
    await runtime.dispatch({"s1": _delivery(_value(2.0), _value(3.0))})
    assert runtime.points_routed == 1
    assert runtime.points_dropped == 2


async def test_drop_old_evicts_oldest():
    sink = FakeSink()
    runtime = _runtime({"s1": sink}, policy="drop_old", maxsize=1)
    await runtime.dispatch({"s1": _delivery(_value(1.0))})
    await runtime.dispatch({"s1": _delivery(_value(2.0))})
    assert runtime.points_routed == 2
    assert runtime.points_dropped == 1
    assert runtime.queue_depths() == {"s1": 1}


async def test_block_policy_applies_backpressure():
    sink = FakeSink()
    runtime = _runtime({"s1": sink}, policy="block", maxsize=1)
    await runtime.dispatch({"s1": _delivery(_value(1.0))})
    blocked = asyncio.create_task(runtime.dispatch({"s1": _delivery(_value(2.0))}))
    await asyncio.sleep(0.05)
    assert not blocked.done()  # 队列满 → 挂起施加背压
    runtime._queues["s1"].get_nowait()  # 手动腾出空间
    await asyncio.wait_for(blocked, timeout=1.0)
    assert runtime.points_routed == 2


async def test_dispatch_unknown_sink_ignored():
    runtime = _runtime({})
    await runtime.dispatch({"ghost": _delivery(_value())})
    assert runtime.points_routed == 0


async def test_stop_drains_queue_before_close():
    sink = FakeSink()
    runtime = _runtime({"s1": sink})
    await runtime.start()
    await runtime.dispatch({"s1": _delivery(_value(1.0))})
    await runtime.stop()
    assert len(sink.batches) == 1  # 停机前排空已入队数据
    assert sink.close_calls == 1


async def test_add_sink_after_start_opens_and_consumes():
    runtime = _runtime({})
    await runtime.start()
    sink = FakeSink()

    await runtime.add_sink("s1", sink)
    assert sink.open_calls == 1
    await runtime.dispatch({"s1": _delivery(_value())})
    await asyncio.sleep(0.05)
    assert len(sink.batches) == 1
    await runtime.stop()


async def test_add_sink_open_failure_leaves_registry_unchanged():
    runtime = _runtime({})
    await runtime.start()
    sink = FakeSink()
    sink.fail_open = True

    with pytest.raises(ConnectionError):
        await runtime.add_sink("s1", sink)
    assert "s1" not in runtime.sinks
    await runtime.stop()


async def test_remove_sink_stops_consumer_and_closes():
    sink = FakeSink()
    runtime = _runtime({"s1": sink})
    await runtime.start()
    await runtime.remove_sink("s1")
    assert "s1" not in runtime.sinks
    assert sink.close_calls == 1 and sink.flush_calls == 1
    assert runtime.queue_depths() == {}
    await runtime.stop()


async def test_rebuild_open_first_for_regular_sink():
    old = FakeSink()
    runtime = _runtime({"s1": old})
    await runtime.start()
    new = FakeSink()

    await runtime.rebuild_sink("s1", new)
    assert new.open_calls == 1
    assert old.close_calls == 1
    assert runtime.sinks["s1"] is new
    await runtime.stop()


async def test_rebuild_close_first_for_exclusive_sink_and_restores_on_failure():
    old = FakeSink(exclusive=True)
    runtime = _runtime({"s1": old})
    await runtime.start()
    new = FakeSink(exclusive=True)
    new.fail_open = True

    with pytest.raises(ConnectionError):
        await runtime.rebuild_sink("s1", new)
    # close-first：旧实例已关闭；open 失败后尽力恢复旧实例
    assert old.close_calls == 1
    assert old.open_calls == 2  # start + restore
    assert runtime.sinks["s1"] is old
    await runtime.stop()


async def test_apply_diff_requires_factory_before_any_removal():
    sink = FakeSink()
    runtime = _runtime({"old": sink})
    await runtime.start()

    new_cfg = Sink(sink_id="new", kind="file", enabled=True, connection={"path": "/tmp/z.csv"})
    diff = SinkDiff(added=["new"], removed=["old"])
    with pytest.raises(RuntimeError, match="sink factory"):
        await runtime.apply_diff(diff, {"new": new_cfg})
    assert "old" in runtime.sinks  # 未执行任何删除
    await runtime.stop()


async def test_apply_diff_add_remove_update():
    old = FakeSink()
    runtime = _runtime({"keep": old, "drop": FakeSink()})
    await runtime.start()

    made: list[FakeSink] = []

    def factory(cfg):
        sink = FakeSink()
        made.append(sink)
        return sink

    runtime._sink_factory = factory
    cfg_keep = Sink(sink_id="keep", kind="file", enabled=True, connection={"path": "/tmp/k2.csv"})
    cfg_new = Sink(sink_id="new", kind="file", enabled=True, connection={"path": "/tmp/n.csv"})
    diff = SinkDiff(added=["new"], removed=["drop"], updated=["keep"])
    await runtime.apply_diff(diff, {"keep": cfg_keep, "new": cfg_new})
    assert set(runtime.sinks) == {"keep", "new"}
    assert "drop" not in runtime.sinks
    assert len(made) == 2
    await runtime.stop()


async def test_unhealthy_sink_dispatch_dropped_not_blocked():
    """open 失败的 unhealthy Sink 无消费者——block 策略下 dispatch 必须丢弃而非挂起。"""
    bad = FakeSink()
    bad.fail_open = True
    runtime = _runtime({"bad": bad}, policy="block", maxsize=1)
    await runtime.start()
    await runtime.dispatch({"bad": _delivery(_value(1.0))})
    await runtime.dispatch({"bad": _delivery(_value(2.0))})  # 修复前：此处永久阻塞
    assert runtime.points_routed == 0
    assert runtime.points_dropped == 2


async def test_stop_with_full_unhealthy_queue_does_not_hang():
    """unhealthy Sink 队列有残留时 stop 不向无消费者队列投哨兵。"""
    bad = FakeSink()
    bad.fail_open = True
    runtime = _runtime({"bad": bad}, policy="block", maxsize=1)
    await runtime.start()
    runtime._queues["bad"].put_nowait(_delivery(_value()))  # 模拟残留数据
    await asyncio.wait_for(runtime.stop(), timeout=2.0)


async def test_blocked_dispatch_wakes_when_sink_removed():
    """block 策略等待中的 dispatch 在 sink 被移除后必须苏醒并丢弃批次。"""
    sink = FakeSink()
    runtime = _runtime({"s1": sink}, policy="block", maxsize=1)
    await runtime.start()

    write_release = asyncio.Event()
    original_write = sink.write

    async def slow_write(batch):
        await write_release.wait()
        await original_write(batch)

    sink.write = slow_write  # type: ignore[method-assign]
    await runtime.dispatch({"s1": _delivery(_value(1.0))})  # 消费者取走后卡在 write
    await asyncio.sleep(0.05)
    await runtime.dispatch({"s1": _delivery(_value(2.0))})  # 填满队列
    blocked = asyncio.create_task(runtime.dispatch({"s1": _delivery(_value(3.0))}))
    await asyncio.sleep(0.05)
    assert not blocked.done()

    await runtime.remove_sink("s1")  # 消费者取消、队列移除
    await asyncio.wait_for(blocked, timeout=2.0)  # 修复前：永久阻塞
    assert runtime.points_dropped == 1
    write_release.set()


async def test_blocked_dispatch_recovers_after_unhealthy_marked():
    """独占重建失败标记 unhealthy 后，阻塞中的 dispatch 苏醒并按丢弃处理。"""
    sink = FakeSink()
    runtime = _runtime({"s1": sink}, policy="block", maxsize=1)
    await runtime.start()

    write_release = asyncio.Event()
    original_write = sink.write

    async def slow_write(batch):
        await write_release.wait()
        await original_write(batch)

    sink.write = slow_write  # type: ignore[method-assign]
    await runtime.dispatch({"s1": _delivery(_value(1.0))})  # 消费者取走后卡在 write
    await asyncio.sleep(0.05)
    await runtime.dispatch({"s1": _delivery(_value(2.0))})  # 填满队列
    blocked = asyncio.create_task(runtime.dispatch({"s1": _delivery(_value(3.0))}))
    await asyncio.sleep(0.05)
    assert not blocked.done()

    runtime._unhealthy.add("s1")  # 模拟独占重建失败的 unhealthy 标记
    await asyncio.wait_for(blocked, timeout=2.0)
    assert runtime.points_dropped == 1

    write_release.set()
    runtime._unhealthy.discard("s1")
    await runtime.stop()


async def test_stop_with_hung_consumer_and_full_queue_does_not_hang():
    """消费者卡死且队列满：哨兵投递超时后取消消费者，stop 有界完成。"""
    sink = FakeSink()

    async def blocked_write(_batch):
        await asyncio.Event().wait()

    sink.write = blocked_write  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    runtime = _runtime({"s1": sink}, maxsize=1)
    await runtime.start()
    await runtime.dispatch({"s1": _delivery(_value(1.0))})  # 消费者取走后卡在 write
    await asyncio.sleep(0.05)
    await runtime.dispatch({"s1": _delivery(_value(2.0))})  # 填满队列

    await asyncio.wait_for(runtime.stop(), timeout=5.0)

    assert sink.close_calls == 1


async def test_stop_propagates_outer_cancellation():
    """外层硬超时取消 stop 时必须传播 CancelledError，不能吞掉后拖延。"""
    sink = FakeSink()

    async def blocked_write(_batch):
        await asyncio.Event().wait()

    sink.write = blocked_write  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    runtime = _runtime({"s1": sink}, maxsize=1)
    await runtime.start()
    await runtime.dispatch({"s1": _delivery(_value(1.0))})
    await asyncio.sleep(0.05)
    await runtime.dispatch({"s1": _delivery(_value(2.0))})

    stop_task = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0.05)
    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    # 被取消的 stop 遗留后台任务；stop 幂等，再停一次清理现场。
    await runtime.stop()


async def test_stop_survives_consumer_unregistering_during_await_phase():
    """消费者在 stop 等待窗口内自行注销（write 失败退出语义）不得中断关闭链。

    确定性编排（不依赖随机 sleep 碰运气）：
    - s1 消费者永远卡在 write，stop 对其哨兵/等待在 shutdown_timeout(0.2s)
      后超时取消；
    - s2 消费者卡在 write 闸门上，stop 进入任务等待循环、跨过 s1 之后
      才放行——s2 写完批次、取出哨兵自行退出，并在 finally 中把自己从
      _consumer_tasks 注销。
    旧实现直接遍历活动字典，循环末尾再取 __next__ 时抛
    RuntimeError("dictionary changed size")，导致 s2 的 flush/close 被跳过。
    """
    hung = FakeSink()
    gated = FakeSink()
    release_s2 = asyncio.Event()
    s2_entered = asyncio.Event()

    async def hung_write(_batch):
        await asyncio.Event().wait()

    async def gated_write(_batch):
        s2_entered.set()
        await release_s2.wait()

    hung.write = hung_write  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    gated.write = gated_write  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    runtime = SinkRuntime(
        {"s1": hung, "s2": gated},
        RuntimeParams(  # type: ignore[arg-type]
            queue_maxsize=2,
            backpressure_policy="drop_old",
            shutdown_timeout=0.2,
        ),
    )
    await runtime.start()
    await runtime.dispatch({"s1": _delivery(_value(1.0)), "s2": _delivery(_value(2.0))})
    await s2_entered.wait()

    stop_task = asyncio.create_task(runtime.stop())
    # 等待 stop 跨过 s1 的 shutdown_timeout 超时、进入对 s2 的 await。
    await asyncio.sleep(0.35)
    release_s2.set()
    await asyncio.wait_for(stop_task, timeout=5.0)

    assert hung.close_calls == 1
    assert gated.flush_calls == 1 and gated.close_calls == 1
    assert not runtime._consumer_tasks  # noqa: SLF001 — 停机后无残留登记
    pending = [task for task in asyncio.all_tasks() if not task.done()]
    assert not [task for task in pending if "SinkRuntime" in str(task)]


async def test_watchdog_stops_consumer_before_reopening_failed_sink():
    sink = FakeSink()
    runtime = _runtime({"s1": sink})
    await runtime.start()
    sink.fail_open = False
    # 通过人为使健康检查失败，触发一次 Watchdog close/open。
    original_health = sink.health
    failed = True

    async def probe():
        nonlocal failed
        if failed:
            failed = False
            from core.application import ConnectionHealth

            return ConnectionHealth(healthy=False, message="connection lost")
        return await original_health()

    runtime._watchdogs["s1"]._health = probe
    await runtime._watchdogs["s1"].check_once()
    assert sink.close_calls == 1
    assert sink.open_calls == 2
    assert "s1" not in runtime._unhealthy
    await runtime.dispatch({"s1": _delivery(_value(3.0))})
    await asyncio.sleep(0.05)
    assert len(sink.batches) == 1
    await runtime.stop()


async def test_unhealthy_sink_drops_counted_per_sink():
    sink = FakeSink()
    sink.fail_open = True
    runtime = _runtime({"s1": sink})
    await runtime.start()
    await runtime.dispatch({"s1": _delivery(_value(), _value())})
    assert runtime.drops_by_sink() == {"s1": 2}
    assert runtime.points_dropped == 2
    await runtime.stop()


async def test_write_failure_counted_and_marked_with_stable_stage():
    attempted = asyncio.Event()

    class FlakySink(FakeSink):
        async def write(self, batch):
            attempted.set()
            raise ConnectionError("sink unreachable")

    runtime = _runtime({"s1": FlakySink()})
    await runtime.start()
    # 等 watchdog 启动后的首次健康检查落地：写失败标记 unhealthy 后，
    # 下一轮检查会立即触发 close/open 重连并在成功时清除标记（设计如此），
    # 这里只需在标记发生前让首个检查周期过去，断言窗口内不再有重连。
    await asyncio.sleep(0.05)
    await runtime.dispatch({"s1": _delivery(_value())})
    await asyncio.wait_for(attempted.wait(), timeout=1.0)
    assert runtime.write_failures() == {"s1": 1}
    assert "s1" in runtime._unhealthy
    assert runtime._unhealthy_reason["s1"].startswith("write_failed:")
    await runtime.stop()
