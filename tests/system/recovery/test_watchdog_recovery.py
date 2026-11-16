"""Recovery：v1 Watchdog 周期建连的真实行为验证。

v1 中设备未连接时采集循环不调度，建连只由 Watchdog 周期驱动
（设计 R-04：普通读路径不盲目创建连接）：

- 设备不可达时 ``collector.poll.executions`` 恒为 0——采集 tick 绝不
  触发建连；每个 Watchdog 周期一次受控 connect 尝试；
- 尝试节拍为 ``watchdog_interval``（0.5s），远稀于 0.1s 采集节拍
  （14s 窗口约 28 次，而按 tick 建连会有约 140 次）；
- 恢复后数据面恢复（设备在线 + Redis 出数）；第二次故障期间
  Watchdog 仍周期重试。

观测点：GetEvents 的 ``DEVICE_RUNTIME_FAILED`` 诊断事件（v1 无独立
建连失败计数器，每次 Watchdog connect 失败记一条事件）与
GetMetrics 的 ``collector.poll.executions`` 稀疏计数。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.collector_config_tree import build_collector_tree_from_site
from tests.support.config_helper import write_config_tree
from tests.support.control import metric_events, metrics_snapshot, start_task_instance
from tests.support.functional_config import realtime_messaging
from tests.support.process import CollectorProcess, free_port
from tests.support.recovery_helpers import DEVICE_ID, telemetry_task, wait_status
from tests.support.sink_harness import latest_value_key
from tests.support.wait import wait_redis_value

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

# v1：task_id == device_id（设备粒度 Task）。
TASK_ID = DEVICE_ID
INSTANCE_ID = DEVICE_ID

#: 采集周期 0.1s：14s 窗口约 140 个 tick——若每次 tick 都 connect 会得到
#: 约 140 次尝试；Watchdog 0.5s 节拍下约 28 次，数量级可清晰区分。
FAST_INTERVAL = 0.1
OUTAGE_WINDOW = 14.0
SAMPLE_INTERVAL = 0.25
#: Watchdog 节拍上限（个位数偏差容忍）：14s / 0.5s ≈ 28 次尝试。
MAX_ATTEMPTS = 40


def _write_fast_config(base: Path, port: int, redis_address: str, nats_url: str) -> Path:
    from tests.support.functional_config import MODBUS_POINTS, modbus_device_dict

    task = telemetry_task()
    task[TASK_ID]["interval"] = FAST_INTERVAL
    return write_config_tree(
        base,
        devices=modbus_device_dict(port),
        point_tables={"modbus": {"points": dict(MODBUS_POINTS)}},
        tasks=task,
        system={
            "runtime": {"connect_timeout": 1.0, "read_timeout": 2.0},
            "messaging": realtime_messaging(nats_url),
        },
    )


async def _sample_attempts(proc: CollectorProcess, window: float) -> tuple[list[float], int, int]:
    """在窗口内采样 Watchdog connect 尝试（诊断事件口径）。

    v1 无独立建连失败计数器：设备从未可达时 ``device_connect_failures``
    （映射自 disconnections）恒为 0；每个 Watchdog 周期的 connect 失败即时
    记一条 ``DEVICE_RUNTIME_FAILED`` ERROR 事件，以事件计数为尝试口径。

    Returns:
        (尝试发生的相对时刻列表, 窗口内尝试次数, 窗口内采集 tick 数)。
    """

    async def _probe() -> tuple[int, int]:
        events = await metric_events(
            proc.grpc_target,
            device_ids=(DEVICE_ID,),
            event_types=("DEVICE_RUNTIME_FAILED",),
        )
        snap = await metrics_snapshot(proc.grpc_target)
        # 计数器稀疏：未发生过的计数不出现在 counters 字典里（如断连期间
        # poll.executions 从未递增），必须按缺失容错取 0。
        return len(events), int(snap["counters"].get("collector.poll.executions", 0))

    base_attempts, base_runs = await _probe()
    attempts_at: list[float] = []
    started = time.monotonic()
    last_seen = base_attempts
    while time.monotonic() - started < window:
        await asyncio.sleep(SAMPLE_INTERVAL)
        attempts, _ = await _probe()
        for _ in range(attempts - last_seen):
            attempts_at.append(time.monotonic() - started)
        last_seen = attempts

    final_attempts, final_runs = await _probe()
    return (
        attempts_at,
        final_attempts - base_attempts,
        final_runs - base_runs,
    )


class TestWatchdogRecovery:
    async def test_watchdog_recovers_without_poll_loop_reconnect(
        self,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
    ) -> None:
        port = free_port()
        server = ModbusMockServer(port=port)
        flat_dir = _write_fast_config(tmp_path / "cfg", port, redis_service, nats_service)
        await sink_service_factory(namespace="wind-hub-watchdog")
        v2_tree = build_collector_tree_from_site(tmp_path / "v2", flat_dir, nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, instance_id=INSTANCE_ID)

        # ---- 故障：设备全程不可达，验证周期连接检查 ----
        _, attempts, runs = await _sample_attempts(proc, OUTAGE_WINDOW)

        # v1：建连只由 Watchdog 周期驱动；设备未连接时采集循环根本不调度
        # （collector.poll.executions 恒为 0），结构上不可能由 poll loop 触发重连。
        # 0.1s 采集节拍下若每 tick 建连，14s 会有约 140 次尝试；Watchdog
        # 0.5s 节拍下约 28 次。
        assert (
            1 <= attempts <= MAX_ATTEMPTS
        ), f"expected periodic watchdog reconnect attempts, got {attempts}"
        assert runs == 0, (
            f"acquisition loop must not run while disconnected: {runs} ticks, "
            f"{attempts} connect attempts"
        )

        # ---- 恢复：重连成功，数据面恢复 ----
        try:
            await server.start()
            await wait_status(proc, lambda p: p["device_connected"] == 1, description="reconnected")

            # 首次建连不记 reconnections（该计数仅统计丢连后恢复）；数据面
            # 恢复以设备在线 + Redis 出数为系统边界事实。
            key = latest_value_key("wind-hub-watchdog", device_id=DEVICE_ID, point_id="rotor_speed")
            value = await wait_redis_value(redis_service, key)
            assert value["quality"] == "good"

            # 二次故障后仍由同一个 Watchdog 周期执行连接检查。
            await server.stop()
            await wait_status(
                proc, lambda p: p["device_connected"] == 0, description="second outage"
            )
            _, second_attempts, _ = await _sample_attempts(proc, 11.0)
            assert (
                second_attempts >= 1
            ), f"watchdog did not retry during second outage: {second_attempts}"
        finally:
            await server.stop()
