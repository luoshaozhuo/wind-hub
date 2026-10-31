"""R5-1：Collector → NATS Core → Sink Service → Redis 真实链路。

全部断言读取真实 Redis 内容（独立测试连接），不检查 Sink 返回值代替。
覆盖：正常更新、多 Sink 路由、过期拒绝、迟到旧值、等时间戳冲突、
PXAT 绝对过期、管理面 Stop/Start/Restart、Redis 重启期间 realtime
丢失语义与恢复后的错误报告/自动恢复。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import start_task_instance
from tests.support.process import CollectorProcess, free_port
from tests.support.redis_client import RedisTestClient, point_key
from tests.support.v2_config import emit_v2_tree_from_flat_site
from tests.support.wait import read_redis_value, wait_redis_value, wait_until
from tests.system.r5.conftest import DEVICE_ID, make_sample, mgmt, publish, wait_state
from tests.system.r5.r5_config import INSTANCE_ID, TASK_ID, write_r5_config

pytestmark = [pytest.mark.redis, pytest.mark.real_service, pytest.mark.system]

POINT = "rotor_speed"


def _redis_sink(
    host: str, port: int, *, sink_id: str, prefix: str, task_id: str = TASK_ID
) -> dict[str, Any]:
    return {
        sink_id: {
            "type": "redis",
            "connection": {"host": host, "port": port, "key_prefix": prefix, "timeout": 1.0},
            "outputs": {task_id: {}},
        }
    }


def _split(address: str) -> tuple[str, int]:
    host, _, port = address.rpartition(":")
    return host, int(port)


async def test_full_chain_collector_to_redis(
    modbus_server: ModbusMockServer,
    collector_factory,
    r5_sink_factory,
    r5_stack,
    tmp_path: Path,
) -> None:
    host, port = _split(r5_stack.redis)
    # v2 采集 task_id == device_id；Sink 的 outputs 授权按 task_id 过滤，
    # 扁平 Sink 配置必须用 v2 task 身份（modbus-1）才能接收该设备的数据。
    v2_task = {
        DEVICE_ID: {
            "device_group_id": DEVICE_ID,
            "point_group": "telemetry",
            "interval": 0.2,
        }
    }
    config_dir = write_r5_config(
        tmp_path / "cfg",
        modbus_server.port,
        r5_stack.nats,
        _redis_sink(host, port, sink_id="redis_sink", prefix="wh-r5-chain", task_id=DEVICE_ID),
        tasks=v2_task,
    )
    await r5_sink_factory(config_dir, realtime_sinks=("redis_sink",))
    # Collector v2 只消费三路径树：从扁平 r5 站点派生（Sink 仍读扁平目录）。
    v2_tree = emit_v2_tree_from_flat_site(tmp_path / "v2", config_dir, nats_url=r5_stack.nats)
    collector: CollectorProcess = await collector_factory(v2_tree, collector_id="collector")
    await start_task_instance(collector.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)

    key = point_key(DEVICE_ID, POINT, prefix="wh-r5-chain")
    first = await wait_redis_value(r5_stack.redis, key, timeout=30.0)
    assert first["quality"] == "good"
    assert isinstance(first["value"], int | float)

    # mock 从站寄存器值静态，持续采样的推进体现在时间戳而非数值。
    first_ts = first["timestamp"]

    async def _advances() -> dict[str, Any] | None:
        current = await read_redis_value(r5_stack.redis, key)
        if current is not None and current["timestamp"] > first_ts:
            return current
        return None

    later = await wait_until(_advances, timeout=20.0)
    assert later["timestamp"] > first["timestamp"]
    assert later["quality"] == "good"


async def test_two_sinks_both_receive_independent_prefixes(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    host, port = _split(r5_stack.redis)
    sinks = {
        **_redis_sink(host, port, sink_id="redis_a", prefix="wh-r5-multi-a"),
        **_redis_sink(host, port, sink_id="redis_b", prefix="wh-r5-multi-b"),
    }
    config_dir = write_r5_config(tmp_path / "cfg", free_port(), r5_stack.nats, sinks)
    sink = await r5_sink_factory(config_dir, realtime_sinks=("redis_a", "redis_b"))
    await wait_state(sink, "redis_a", "running")
    await wait_state(sink, "redis_b", "running")

    await publish(r5_stack, make_sample(POINT, 12.5), "realtime")
    key_a = point_key(DEVICE_ID, POINT, prefix="wh-r5-multi-a")
    key_b = point_key(DEVICE_ID, POINT, prefix="wh-r5-multi-b")
    value_a = await wait_redis_value(r5_stack.redis, key_a)
    value_b = await wait_redis_value(r5_stack.redis, key_b)
    assert value_a["value"] == pytest.approx(12.5)
    assert value_b["value"] == pytest.approx(12.5)


async def test_expired_sample_rejected_then_fresh_accepted(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    host, port = _split(r5_stack.redis)
    config_dir = write_r5_config(
        tmp_path / "cfg",
        free_port(),
        r5_stack.nats,
        _redis_sink(host, port, sink_id="redis_sink", prefix="wh-r5-expiry"),
    )
    sink = await r5_sink_factory(config_dir, realtime_sinks=("redis_sink",))
    await wait_state(sink, "redis_sink", "running")

    stale = make_sample(
        POINT,
        999.0,
        sample_id="stale-sample",
        sample_time=datetime.now(UTC) - timedelta(seconds=30),
        max_staleness_ms=1_000,
    )
    await publish(r5_stack, stale, "realtime")

    fresh = make_sample(POINT, 42.0)
    await publish(r5_stack, fresh, "realtime")
    key = point_key(DEVICE_ID, POINT, prefix="wh-r5-expiry")
    value = await wait_redis_value(r5_stack.redis, key, match=lambda v: v["value"] == 42.0)
    assert value["sample_id"] == fresh.sample_id


async def test_late_old_value_does_not_overwrite_newer(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    host, port = _split(r5_stack.redis)
    config_dir = write_r5_config(
        tmp_path / "cfg",
        free_port(),
        r5_stack.nats,
        _redis_sink(host, port, sink_id="redis_sink", prefix="wh-r5-late"),
    )
    sink = await r5_sink_factory(config_dir, realtime_sinks=("redis_sink",))
    await wait_state(sink, "redis_sink", "running")

    base_time = datetime.now(UTC)
    newer = make_sample(POINT, 100.0, sample_id="newer", sample_time=base_time)
    older = make_sample(POINT, 1.0, sample_id="older", sample_time=base_time - timedelta(seconds=5))
    await publish(r5_stack, newer, "realtime")
    key = point_key(DEVICE_ID, POINT, prefix="wh-r5-late")
    await wait_redis_value(r5_stack.redis, key, match=lambda v: v["sample_id"] == "newer")

    await publish(r5_stack, older, "realtime")
    # 留出处理时间后，值不得回滚。
    with pytest.raises(TimeoutError):
        await wait_redis_value(
            r5_stack.redis, key, timeout=2.0, match=lambda v: v["sample_id"] == "older"
        )
    current = await read_redis_value(r5_stack.redis, key)
    assert current["sample_id"] == "newer"
    assert current["value"] == 100.0


async def test_equal_timestamp_conflicting_samples_fail_sink(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    host, port = _split(r5_stack.redis)
    config_dir = write_r5_config(
        tmp_path / "cfg",
        free_port(),
        r5_stack.nats,
        _redis_sink(host, port, sink_id="redis_sink", prefix="wh-r5-conflict"),
    )
    sink = await r5_sink_factory(config_dir, realtime_sinks=("redis_sink",))
    await wait_state(sink, "redis_sink", "running")

    exact_time = datetime.now(UTC)
    first = make_sample(POINT, 7.0, sample_id="same-ts-1", sample_time=exact_time)
    second = make_sample(POINT, 8.0, sample_id="same-ts-2", sample_time=exact_time)
    await publish(r5_stack, first, "realtime")
    key = point_key(DEVICE_ID, POINT, prefix="wh-r5-conflict")
    await wait_redis_value(r5_stack.redis, key, match=lambda v: v["sample_id"] == "same-ts-1")
    await publish(r5_stack, second, "realtime")

    # 设计语义：等时间戳歧义是消息级拒绝——Sink 保持 running（后续合法消息
    # 不被拖死），拒绝原因必须在管理面持续记账，且 Redis 现值不被覆盖。
    async def _rejected() -> dict[str, Any] | None:
        status = await mgmt(sink, "GetSinkStatus", "redis_sink")
        if status["failure"] and "equal-time" in str(status["failure"]):
            return status
        return None

    rejected = await wait_until(_rejected, timeout=5.0, interval=0.1)
    assert rejected["state"] == "running"
    current = await read_redis_value(r5_stack.redis, key)
    assert current["sample_id"] == "same-ts-1"
    assert current["value"] == 7.0

    # 被拒绝后合法新时间戳消息仍正常落库（拒绝未毒化消费链路）。
    # 时间戳必须在发布当时取墙钟：用 exact_time+1s 会在观察到拒绝（亚秒级）
    # 后落入未来时间戳，被 _is_fresh 的时钟漂移防护正确拒绝。
    recovered = make_sample(POINT, 9.0, sample_id="after-conflict", sample_time=datetime.now(UTC))
    assert recovered.sample_time > exact_time
    await publish(r5_stack, recovered, "realtime")
    await wait_redis_value(r5_stack.redis, key, match=lambda v: v["sample_id"] == "after-conflict")


async def test_pxat_absolute_expiry_deletes_key(r5_sink_factory, r5_stack, tmp_path: Path) -> None:
    host, port = _split(r5_stack.redis)
    config_dir = write_r5_config(
        tmp_path / "cfg",
        free_port(),
        r5_stack.nats,
        _redis_sink(host, port, sink_id="redis_sink", prefix="wh-r5-ttl"),
    )
    sink = await r5_sink_factory(config_dir, realtime_sinks=("redis_sink",))
    await wait_state(sink, "redis_sink", "running")

    sample = make_sample(POINT, 55.0, sample_id="ttl-1", max_staleness_ms=1_500)
    await publish(r5_stack, sample, "realtime")
    key = point_key(DEVICE_ID, POINT, prefix="wh-r5-ttl")
    await wait_redis_value(r5_stack.redis, key)

    async with RedisTestClient(r5_stack.redis) as client:
        pttl = await client._request("PTTL", key)
    assert isinstance(pttl, int) and 0 < pttl <= 1_500

    async def _gone() -> bool | None:
        if await read_redis_value(r5_stack.redis, key) is None:
            return True
        return None

    await wait_until(_gone, timeout=6.0)


async def test_management_stop_start_restart(r5_sink_factory, r5_stack, tmp_path: Path) -> None:
    host, port = _split(r5_stack.redis)
    config_dir = write_r5_config(
        tmp_path / "cfg",
        free_port(),
        r5_stack.nats,
        _redis_sink(host, port, sink_id="redis_sink", prefix="wh-r5-mgmt"),
    )
    sink = await r5_sink_factory(config_dir, realtime_sinks=("redis_sink",))
    await wait_state(sink, "redis_sink", "running")

    await publish(r5_stack, make_sample(POINT, 1.0, sample_id="before-stop"), "realtime")
    key = point_key(DEVICE_ID, POINT, prefix="wh-r5-mgmt")
    await wait_redis_value(r5_stack.redis, key, match=lambda v: v["value"] == 1.0)

    stopped = await mgmt(sink, "StopSink", "redis_sink")
    assert stopped["state"] == "stopped"
    await publish(r5_stack, make_sample(POINT, 2.0, sample_id="while-stopped"), "realtime")
    with pytest.raises(TimeoutError):
        await wait_redis_value(
            r5_stack.redis, key, timeout=2.0, match=lambda v: v["sample_id"] == "while-stopped"
        )

    started = await mgmt(sink, "StartSink", "redis_sink")
    assert started["state"] == "running"
    await publish(r5_stack, make_sample(POINT, 3.0, sample_id="after-start"), "realtime")
    await wait_redis_value(r5_stack.redis, key, match=lambda v: v["sample_id"] == "after-start")

    restarted = await mgmt(sink, "RestartSink", "redis_sink")
    assert restarted["state"] == "running"
    await publish(r5_stack, make_sample(POINT, 4.0, sample_id="after-restart"), "realtime")
    await wait_redis_value(r5_stack.redis, key, match=lambda v: v["sample_id"] == "after-restart")


async def test_redis_restart_realtime_loss_reports_and_recovers(
    r5_sink_factory, r5_stack, tmp_path: Path
) -> None:
    host, port = _split(r5_stack.redis)
    config_dir = write_r5_config(
        tmp_path / "cfg",
        free_port(),
        r5_stack.nats,
        _redis_sink(host, port, sink_id="redis_sink", prefix="wh-r5-outage"),
    )
    sink = await r5_sink_factory(config_dir, realtime_sinks=("redis_sink",))
    await wait_state(sink, "redis_sink", "running")
    key = point_key(DEVICE_ID, POINT, prefix="wh-r5-outage")

    r5_stack.stop_container("redis")
    try:
        # 故障窗口内 realtime 消息允许丢失，但 Sink 必须进入 failed 并持续记账。
        await publish(r5_stack, make_sample(POINT, 900.0, sample_id="lost-1"), "realtime")

        async def _failed_during_outage() -> dict[str, Any] | None:
            status = await mgmt(sink, "GetSinkStatus", "redis_sink")
            if status["state"] == "failed":
                return status
            return None

        failed = await wait_until(_failed_during_outage, timeout=8.0, interval=0.2)
        assert failed["failure"]
    finally:
        r5_stack.start_container("redis")

    # supervisor 每 5s 自动恢复；恢复后新消息落库，无需人工重启。
    recovered = await wait_state(sink, "redis_sink", "running", timeout=40.0)
    assert recovered["failure"] is None
    await publish(r5_stack, make_sample(POINT, 200.0, sample_id="after-recovery"), "realtime")
    value = await wait_redis_value(
        r5_stack.redis, key, timeout=20.0, match=lambda v: v["sample_id"] == "after-recovery"
    )
    assert value["value"] == 200.0
