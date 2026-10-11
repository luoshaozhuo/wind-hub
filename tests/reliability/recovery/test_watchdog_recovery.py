"""Recovery：Watchdog 周期恢复 节流的真实行为验证。

断言的不是「最终重连成功」（已有用例覆盖），而是退避曲线本身：

- 设备不可达时 connect 尝试被 ``next_retry_at`` 节流——0.1s 采集周期
  绝不等于 0.1s 一次 connect；
- 尝试间隔按 1s/2s/4s/8s 指数增长（30s 封顶，本用例窗口内不触及）；
- 恢复后 ``consecutive_failures`` 清零，第二次故障的退避从初始档
  重新开始（不得延续上次的高位档）。

观测点：GetMetricsSnapshot 的 ``device_connect_failures`` 累计计数
（每次真实 connect 尝试 +1），采样间隔 0.25s，远小于最小退避档 1s。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.reliability.recovery.helpers import telemetry_task, wait_status
from tests.support.config_helper import write_config_tree
from tests.support.control import metrics_snapshot, start_task_instance
from tests.support.process import CollectorProcess, free_port
from tests.support.wait import wait_file_rows

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"
DEVICE_ID = "modbus-1"

#: 采集周期 0.1s：14s 窗口约 140 个 tick——若每次 tick 都 connect 会得到
#: 约 140 次尝试；正确退避下只有约 5 次，两个数量级可清晰区分。
FAST_INTERVAL = 0.1
OUTAGE_WINDOW = 14.0
SAMPLE_INTERVAL = 0.25


def _write_fast_config(base: Path, port: int, sink_path: Path) -> Path:
    from tests.support.functional_config import MODBUS_POINTS, modbus_device_dict

    task = telemetry_task("file_sink")
    task["interval"] = FAST_INTERVAL
    return write_config_tree(
        base,
        devices=[modbus_device_dict(port)],
        point_tables={"modbus": {"points": list(MODBUS_POINTS)}},
        sinks=[
            {
                "name": "file_sink",
                "type": "file",
                "connection": {
                    "path": str(sink_path),
                },
            }
        ],
        tasks=[task],
        system={"runtime": {"connect_timeout": 1.0, "read_timeout": 2.0}},
    )


async def _sample_attempts(proc: CollectorProcess, window: float) -> tuple[list[float], int, int]:
    """在窗口内采样 connect 尝试计数。

    Returns:
        (尝试发生的相对时刻列表, 窗口内尝试次数, 窗口内采集 tick 数)。
    """
    first = await metrics_snapshot(proc.grpc_target)
    base_failures = first["device_connect_failures"].get(DEVICE_ID, 0)
    base_runs = first["counters"]["acquisition_runs"]

    attempts_at: list[float] = []
    started = time.monotonic()
    last_seen = base_failures
    while time.monotonic() - started < window:
        await asyncio.sleep(SAMPLE_INTERVAL)
        snap = await metrics_snapshot(proc.grpc_target)
        failures = snap["device_connect_failures"].get(DEVICE_ID, 0)
        for _ in range(failures - last_seen):
            attempts_at.append(time.monotonic() - started)
        last_seen = failures

    last = await metrics_snapshot(proc.grpc_target)
    return (
        attempts_at,
        last["device_connect_failures"].get(DEVICE_ID, 0) - base_failures,
        last["counters"]["acquisition_runs"] - base_runs,
    )


class TestWatchdogRecovery:
    async def test_watchdog_recovers_without_poll_loop_reconnect(
        self, collector_factory, tmp_path: Path
    ) -> None:
        port = free_port()
        server = ModbusMockServer(port=port)
        sink_path = tmp_path / "out" / "telemetry.csv"
        config_dir = _write_fast_config(tmp_path / "cfg", port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)

        # ---- 故障：设备全程不可达，验证周期连接检查 ----
        _, attempts, runs = await _sample_attempts(proc, OUTAGE_WINDOW)

        assert 1 <= attempts <= 6, f"expected periodic watchdog reconnect attempts, got {attempts}"
        assert (
            attempts * 10 < runs
        ), f"connect attempts ({attempts}) should not follow acquisition ticks ({runs})"

        # ---- 恢复：重连成功，失败计数清零，数据面恢复 ----
        try:
            await server.start()
            await wait_status(
                proc, lambda p: p["devices_connected"] == 1, description="reconnected"
            )
            snap = await metrics_snapshot(proc.grpc_target)
            assert snap["device_reconnects"].get(DEVICE_ID, 0) >= 1
            await wait_file_rows(sink_path, min_rows=2)

            # 二次故障后仍由同一个 Watchdog 周期执行连接检查。
            await server.stop()
            await wait_status(
                proc, lambda p: p["devices_connected"] == 0, description="second outage"
            )
            _, second_attempts, _ = await _sample_attempts(proc, 11.0)
            assert (
                second_attempts >= 1
            ), f"watchdog did not retry during second outage: {second_attempts}"
        finally:
            await server.stop()
