"""注入面性能压测：10 Hz × 500 点批 × 多服务器的真实 grpc.aio 负载。

不加入默认收集（performance marker，经 -m performance 显式触发）。
测量 UpdateVariables 往返延迟 P50/P95/P99 与进程 RSS/CPU，不设失败阈值以外
的宽松断言；记录全局配置锁竞争下的实测尾延迟。
"""

from __future__ import annotations

import asyncio
import math
import os
import resource
import time
from pathlib import Path

import pytest

from contracts import industrial_gateway_pb2 as pb
from core.infrastructure.rpc.scalar_codec import encode_scalar
from tests.support.gateway_v1 import (
    ServerSpec,
    reserve_port,
    start_gateway_v1,
)

pytestmark = [pytest.mark.performance, pytest.mark.real_service]

SERVER_COUNT = 5
POINTS_PER_SERVER = 500
HZ = 10
DURATION_SECONDS = 10.0


def _large_point_table() -> dict[str, dict[str, object]]:
    return {
        f"fb_u{index:03d}": {
            "data_type": "uint16",
            "address": {"register_type": "holding", "address": 100 + index},
        }
        for index in range(POINTS_PER_SERVER)
    }


def _specs() -> list[ServerSpec]:
    return [
        ServerSpec(
            server_id=f"gw{index:02d}",
            device_id=f"D{index:03d}",
            modbus_port=reserve_port(),
            slave_id=(index % 247) + 1,
            control_enabled=False,
            command_mappings=(),
        )
        for index in range(SERVER_COUNT)
    ]


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        raise AssertionError("no latency samples")
    rank = max(1, math.ceil(percentile * len(sorted_values)))
    return sorted_values[rank - 1]


async def test_10hz_500point_batches_multi_server(tmp_path: Path) -> None:
    points = _large_point_table()
    point_ids = list(points)
    harness = await start_gateway_v1(
        tmp_path / "root",
        _specs(),
        point_table=points,
        metrics_window_seconds=60,
    )
    latencies_ms: list[float] = []
    batches = 0
    rss_start = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    wall_start = time.perf_counter()
    try:
        interval = 1.0 / HZ
        deadline = wall_start + DURATION_SECONDS
        tick = 0
        while time.perf_counter() < deadline:
            scheduled = wall_start + tick * interval
            delay = scheduled - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tick += 1

            async def one_batch(server_id: str, seq: int) -> None:
                values = [
                    pb.VariableUpdate(
                        variable_id=point_ids[index],
                        value=encode_scalar((seq + index) % 65536),
                    )
                    for index in range(POINTS_PER_SERVER)
                ]
                started = time.perf_counter()
                reply = await harness.gateway_stub().UpdateVariables(
                    pb.UpdateVariablesRequest(
                        gateway_id=server_id,
                        values=values,
                    )
                )
                latencies_ms.append((time.perf_counter() - started) * 1000)
                assert all(result.success for result in reply.results)

            await asyncio.gather(
                *(one_batch(f"gw{index:02d}", tick) for index in range(SERVER_COUNT))
            )
            batches += SERVER_COUNT
    finally:
        await harness.stop()

    wall = time.perf_counter() - wall_start
    ordered = sorted(latencies_ms)
    p50 = _percentile(ordered, 0.50)
    p95 = _percentile(ordered, 0.95)
    p99 = _percentile(ordered, 0.99)
    rss_end = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    points_per_second = batches * POINTS_PER_SERVER / wall
    cpu = os.times()

    print(
        "\nperf: "
        f"batches={batches} points={batches * POINTS_PER_SERVER} wall={wall:.1f}s "
        f"rate={points_per_second:.0f} points/s\n"
        f"latency ms p50={p50:.2f} p95={p95:.2f} p99={p99:.2f} max={ordered[-1]:.2f}\n"
        f"rss_kb start={rss_start} end={rss_end} "
        f"cpu user={cpu.user:.1f}s system={cpu.system:.1f}s"
    )

    # 每服务器 10Hz 节拍：五个服务器 10s 至少完成 90% 的计划批数。
    planned = int(DURATION_SECONDS * HZ) * SERVER_COUNT
    assert batches >= planned * 0.9
    # 无错误批（各批 500 点全部 success 已在循环中断言）。
    assert len(latencies_ms) == batches
