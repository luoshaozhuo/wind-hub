"""Recovery：重连 backoff 节流的真实行为验证。

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
from tests.support.control import apply_placement_and_start_instance, metrics_snapshot
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
                    "buffer_size": 4,
                    "flush_interval": 0.5,
                },
            }
        ],
        tasks=[task],
        system={"runtime": {"connect_timeout": 1.0, "read_timeout": 2.0}},
    )


async def _sample_attempts(
    proc: CollectorProcess, window: float
) -> tuple[list[float], int, int]:
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


class TestReconnectBackoff:
    async def test_backoff_throttles_and_resets_after_recovery(
        self, collector_factory, tmp_path: Path
    ) -> None:
        port = free_port()
        server = ModbusMockServer(port=port)
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = _write_fast_config(tmp_path / "cfg", port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)
        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )

        # ---- 故障：设备全程不可达，观察退避曲线 ----
        attempts_at, attempts, runs = await _sample_attempts(proc, OUTAGE_WINDOW)

        assert 3 <= attempts <= 8, (
            f"expected ~5 throttled connect attempts in {OUTAGE_WINDOW}s, got {attempts}"
        )
        assert attempts * 10 < runs, (
            f"connect attempts ({attempts}) not throttled relative to "
            f"acquisition ticks ({runs})"
        )
        gaps = [b - a for a, b in zip(attempts_at, attempts_at[1:], strict=False)]
        # 启动期的前几次尝试可能落在采样基线之前，窗口内只要求可判型的间隔数。
        assert len(gaps) >= 2, f"too few attempts to inspect backoff gaps: {attempts_at}"
        # 指数退避：后一档不短于前一档（采样误差 ±0.5s，留 0.6 容忍系数），
        # 且末档显著大于 1s 固定重试能达到的间隔。
        for prev, cur in zip(gaps, gaps[1:], strict=False):
            assert cur >= prev * 0.6, f"backoff gaps not non-decreasing: {gaps}"
        assert gaps[-1] >= 2.0, f"final backoff gap too small (no exponential growth): {gaps}"

        # ---- 恢复：重连成功，失败计数清零，数据面恢复 ----
        try:
            await server.start()
            await wait_status(
                proc, lambda p: p["devices_connected"] == 1, description="reconnected"
            )
            snap = await metrics_snapshot(proc.grpc_target)
            assert snap["device_reconnects"].get(DEVICE_ID, 0) >= 1
            await wait_file_rows(sink_path, min_rows=2)

            # ---- 二次故障：退避必须从初始档重新开始 ----
            await server.stop()
            await wait_status(
                proc, lambda p: p["devices_connected"] == 0, description="second outage"
            )
            _, second_attempts, _ = await _sample_attempts(proc, 4.5)
            # 初始档 1s：4.5s 窗口至少 2 次尝试；若延续 30s 封顶档则只有 1 次。
            assert second_attempts >= 2, (
                f"backoff did not reset after recovery: "
                f"{second_attempts} attempts in 4.5s window"
            )
        finally:
            await server.stop()
