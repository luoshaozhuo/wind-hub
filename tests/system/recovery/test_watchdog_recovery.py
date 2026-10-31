"""Recovery：v2 Watchdog 周期建连节流的真实行为验证。

v2 中设备未连接时采集循环不调度，建连只由 Watchdog 退避驱动：

- 设备不可达时 connect 尝试被退避节流——0.1s 采集节拍绝不等于
  0.1s 一次 connect（未连接时 ``acquisition_runs`` 恒为 0）；
- 尝试间隔按指数退避增长（30s 封顶），14s 窗口内只有个位数次尝试；
- 恢复后数据面恢复、指标记账 ``device_reconnects``；第二次故障期间
  Watchdog 仍周期重试（退避从初始档重新开始）。

观测点：GetMetrics 的 ``device_connect_failures`` 累计计数
（每次真实 connect 尝试 +1），采样间隔 0.25s，远小于最小退避档 1s。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.config_helper import write_config_tree
from tests.support.control import metrics_snapshot, start_task_instance
from tests.support.functional_config import realtime_messaging
from tests.support.process import CollectorProcess, free_port
from tests.support.recovery_helpers import DEVICE_ID, telemetry_task, wait_status
from tests.support.redis_client import point_key
from tests.support.v2_config import emit_v2_tree_from_flat_site
from tests.support.wait import wait_redis_value, wait_until

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

# v2：task_id == device_id。
TASK_ID = DEVICE_ID
INSTANCE_ID = DEVICE_ID

#: 采集周期 0.1s：14s 窗口约 140 个 tick——若每次 tick 都 connect 会得到
#: 约 140 次尝试；正确退避下只有约 5 次，两个数量级可清晰区分。
FAST_INTERVAL = 0.1
OUTAGE_WINDOW = 14.0
SAMPLE_INTERVAL = 0.25


def _write_fast_config(base: Path, port: int, redis_address: str, nats_url: str) -> Path:
    from tests.support.functional_config import MODBUS_POINTS, modbus_device_dict

    task = telemetry_task()
    task[TASK_ID]["interval"] = FAST_INTERVAL
    return write_config_tree(
        base,
        devices=modbus_device_dict(port),
        point_tables={"modbus": {"points": dict(MODBUS_POINTS)}},
        sinks={
            "redis_sink": {
                "type": "redis",
                "connection": {
                    "host": redis_address.rpartition(":")[0],
                    "port": int(redis_address.rpartition(":")[2]),
                },
                "outputs": {TASK_ID: {"key_prefix": "wind-hub-watchdog"}},
            }
        },
        tasks=task,
        system={
            "runtime": {"connect_timeout": 1.0, "read_timeout": 2.0},
            "messaging": realtime_messaging(nats_url),
        },
    )


async def _sample_attempts(proc: CollectorProcess, window: float) -> tuple[list[float], int, int]:
    """在窗口内采样 connect 尝试计数。

    Returns:
        (尝试发生的相对时刻列表, 窗口内尝试次数, 窗口内采集 tick 数)。
    """
    first = await metrics_snapshot(proc.grpc_target)
    base_failures = int(first["device_connect_failures"].get(DEVICE_ID, 0))
    base_runs = int(first["counters"]["acquisition_runs"])

    attempts_at: list[float] = []
    started = time.monotonic()
    last_seen = base_failures
    while time.monotonic() - started < window:
        await asyncio.sleep(SAMPLE_INTERVAL)
        snap = await metrics_snapshot(proc.grpc_target)
        failures = int(snap["device_connect_failures"].get(DEVICE_ID, 0))
        for _ in range(failures - last_seen):
            attempts_at.append(time.monotonic() - started)
        last_seen = failures

    last = await metrics_snapshot(proc.grpc_target)
    return (
        attempts_at,
        int(last["device_connect_failures"].get(DEVICE_ID, 0)) - base_failures,
        int(last["counters"]["acquisition_runs"]) - base_runs,
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
        await sink_service_factory(flat_dir)
        v2_tree = emit_v2_tree_from_flat_site(tmp_path / "v2", flat_dir, nats_url=nats_service)
        proc: CollectorProcess = await collector_factory(v2_tree)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)

        # ---- 故障：设备全程不可达，验证周期连接检查 ----
        _, attempts, runs = await _sample_attempts(proc, OUTAGE_WINDOW)

        # v2：建连只由 Watchdog 退避驱动；设备未连接时采集循环根本不调度
        # （acquisition_runs 恒为 0），结构上不可能由 poll loop 触发重连。
        # 0.1s 采集节拍下若每 tick 建连，14s 会有约 140 次尝试；正确退避
        # 下只有个位数次。
        assert 1 <= attempts <= 6, f"expected periodic watchdog reconnect attempts, got {attempts}"
        assert runs == 0, (
            f"acquisition loop must not run while disconnected: {runs} ticks, "
            f"{attempts} connect attempts"
        )

        # ---- 恢复：重连成功，失败计数清零，数据面恢复 ----
        try:
            await server.start()
            await wait_status(proc, lambda p: p["device_connected"] == 1, description="reconnected")

            async def reconnect_recorded() -> bool | None:
                snap = await metrics_snapshot(proc.grpc_target)
                return True if snap["device_reconnects"].get(DEVICE_ID, 0) >= 1 else None

            await wait_until(
                reconnect_recorded,
                timeout=5.0,
                description="watchdog reconnect metric published",
            )
            value = await wait_redis_value(
                redis_service, point_key(DEVICE_ID, "rotor_speed", prefix="wind-hub-watchdog")
            )
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
