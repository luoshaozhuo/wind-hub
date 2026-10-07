"""新 Collector SinkRuntime 队列/背压/消费者/热重建单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from collector.application.config import RuntimeParams
from collector.application.reload import SinkDiff
from collector.application.sink_runtime import SinkRuntime
from collector.domain.point_value import PointValue
from tests.support.new_collector import FakeSink


def _value(n: float = 1.0) -> PointValue:
    return PointValue(device_id="d", point_id="p", value=n)


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

    await runtime.dispatch({"s1": [_value()]})
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
    health = runtime.health()
    assert not health["bad"].healthy and health["bad"].message == "open failed"
    assert health["good"].healthy

    await runtime.dispatch({"good": [_value()]})
    await asyncio.sleep(0.05)
    assert len(good.batches) == 1
    await runtime.stop()


async def test_drop_new_discards_when_full():
    sink = FakeSink()
    runtime = _runtime({"s1": sink}, policy="drop_new", maxsize=1)
    # 不 start → 无消费者 → 队列堆积
    await runtime.dispatch({"s1": [_value(1.0)]})
    await runtime.dispatch({"s1": [_value(2.0), _value(3.0)]})
    assert runtime.points_routed == 1
    assert runtime.points_dropped == 2


async def test_drop_old_evicts_oldest():
    sink = FakeSink()
    runtime = _runtime({"s1": sink}, policy="drop_old", maxsize=1)
    await runtime.dispatch({"s1": [_value(1.0)]})
    await runtime.dispatch({"s1": [_value(2.0)]})
    assert runtime.points_routed == 2
    assert runtime.points_dropped == 1
    assert runtime.queue_depths() == {"s1": 1}


async def test_block_policy_applies_backpressure():
    sink = FakeSink()
    runtime = _runtime({"s1": sink}, policy="block", maxsize=1)
    await runtime.dispatch({"s1": [_value(1.0)]})
    blocked = asyncio.create_task(runtime.dispatch({"s1": [_value(2.0)]}))
    await asyncio.sleep(0.05)
    assert not blocked.done()  # 队列满 → 挂起施加背压
    runtime._queues["s1"].get_nowait()  # 手动腾出空间
    await asyncio.wait_for(blocked, timeout=1.0)
    assert runtime.points_routed == 2


async def test_dispatch_unknown_sink_ignored():
    runtime = _runtime({})
    await runtime.dispatch({"ghost": [_value()]})
    assert runtime.points_routed == 0


async def test_stop_drains_queue_before_close():
    sink = FakeSink()
    runtime = _runtime({"s1": sink})
    await runtime.start()
    await runtime.dispatch({"s1": [_value(1.0)]})
    await runtime.stop()
    assert len(sink.batches) == 1  # 停机前排空已入队数据
    assert sink.close_calls == 1


async def test_add_sink_after_start_opens_and_consumes():
    runtime = _runtime({})
    await runtime.start()
    sink = FakeSink()
    from collector.application.sinks import (
        FileSinkConnection,
        ResolvedSinkConfig,
    )

    cfg = ResolvedSinkConfig(
        name="s1", type="file", connection=FileSinkConnection(path="/tmp/x.jsonl")
    )
    await runtime.add_sink("s1", cfg, sink)
    assert sink.open_calls == 1
    await runtime.dispatch({"s1": [_value()]})
    await asyncio.sleep(0.05)
    assert len(sink.batches) == 1
    await runtime.stop()


async def test_add_sink_open_failure_leaves_registry_unchanged():
    runtime = _runtime({})
    await runtime.start()
    sink = FakeSink()
    sink.fail_open = True
    from collector.application.sinks import FileSinkConnection, ResolvedSinkConfig

    cfg = ResolvedSinkConfig(
        name="s1", type="file", connection=FileSinkConnection(path="/tmp/x.jsonl")
    )
    with pytest.raises(ConnectionError):
        await runtime.add_sink("s1", cfg, sink)
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
    from collector.application.sinks import FileSinkConnection, ResolvedSinkConfig

    cfg = ResolvedSinkConfig(
        name="s1", type="file", connection=FileSinkConnection(path="/tmp/y.jsonl")
    )
    await runtime.rebuild_sink("s1", cfg, new)
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
    from collector.application.sinks import (
        IEC104SinkConnection,
        ResolvedSinkConfig,
    )

    cfg = ResolvedSinkConfig(name="s1", type="iec104", connection=IEC104SinkConnection())
    with pytest.raises(ConnectionError):
        await runtime.rebuild_sink("s1", cfg, new)
    # close-first：旧实例已关闭；open 失败后尽力恢复旧实例
    assert old.close_calls == 1
    assert old.open_calls == 2  # start + restore
    assert runtime.sinks["s1"] is old
    await runtime.stop()


async def test_apply_diff_requires_factory_before_any_removal():
    sink = FakeSink()
    runtime = _runtime({"old": sink})
    await runtime.start()
    from collector.application.sinks import FileSinkConnection, ResolvedSinkConfig

    new_cfg = ResolvedSinkConfig(
        name="new", type="file", connection=FileSinkConnection(path="/tmp/z.jsonl")
    )
    diff = SinkDiff(added=["new"], removed=["old"])
    with pytest.raises(RuntimeError, match="sink factory"):
        await runtime.apply_diff(diff, {"new": new_cfg})
    assert "old" in runtime.sinks  # 未执行任何删除
    await runtime.stop()


async def test_apply_diff_add_remove_update():
    old = FakeSink()
    runtime = _runtime({"keep": old, "drop": FakeSink()})
    await runtime.start()
    from collector.application.sinks import FileSinkConnection, ResolvedSinkConfig

    made: list[FakeSink] = []

    def factory(cfg):
        sink = FakeSink()
        made.append(sink)
        return sink

    runtime._sink_factory = factory
    cfg_keep = ResolvedSinkConfig(
        name="keep", type="file", connection=FileSinkConnection(path="/tmp/k2.jsonl")
    )
    cfg_new = ResolvedSinkConfig(
        name="new", type="file", connection=FileSinkConnection(path="/tmp/n.jsonl")
    )
    diff = SinkDiff(added=["new"], removed=["drop"], updated=["keep"])
    await runtime.apply_diff(diff, {"keep": cfg_keep, "new": cfg_new})
    assert set(runtime.sinks) == {"keep", "new"}
    assert "drop" not in runtime.sinks
    assert len(made) == 2
    await runtime.stop()
