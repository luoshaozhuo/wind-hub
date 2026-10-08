"""Modbus 读取计划微基准：固定选点与动态选点（不包含设备网络 I/O）。

运行：python tests/benchmarks/read_plan_benchmark.py
此基准仅比较点位映射和连续地址分组阶段，不代表实际协议吞吐量。
"""

from __future__ import annotations

import gc
import statistics
import time
import tracemalloc
from collections.abc import Callable

from core.domain import (
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    UNIT_CATALOG,
    UnitCode,
)
from core.infrastructure.protocol.modbus.driver import ModbusDriver


def _driver(point_count: int = 256) -> ModbusDriver:
    unit = UNIT_CATALOG[UnitCode.NONE]
    points = {
        f"p{index}": Point(
            point_id=f"p{index}",
            business_point_id=f"p{index}",
            source_unit=unit,
            access=PointAccess.READ_WRITE,
            ext={
                "register_type": "holding",
                "address": index,
                "data_type": "uint16",
            },
        )
        for index in range(point_count)
    }
    return ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable("benchmark", Protocol("modbus"), points),
        {},
    )


def _measure(fn: Callable[[], None], iterations: int = 5000) -> tuple[float, int]:
    """返回每次读取计划准备耗时（微秒）和峰值分配（字节）。"""
    for _ in range(200):
        fn()
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter_ns()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter_ns() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return elapsed / iterations / 1000, peak


def main() -> None:
    driver = _driver()
    fixed = tuple(f"p{i}" for i in range(64))
    dynamic = [
        tuple(f"p{(i * 17 + j) % 256}" for j in range(32))
        for i in range(64)
    ]

    def fixed_cached() -> None:
        driver._read_plan(fixed)

    def fixed_uncached() -> None:
        driver._read_plan_cache.clear()
        driver._read_plan(fixed)

    index = 0

    def dynamic_cached() -> None:
        nonlocal index
        driver._read_plan(dynamic[index % len(dynamic)])
        index += 1

    def dynamic_uncached() -> None:
        nonlocal index
        driver._read_plan_cache.clear()
        driver._read_plan(dynamic[index % len(dynamic)])
        index += 1

    cases = (
        ("fixed / cached", fixed_cached),
        ("fixed / no cache", fixed_uncached),
        ("dynamic / cached (64 targets)", dynamic_cached),
        ("dynamic / no cache (64 targets)", dynamic_uncached),
    )
    for label, fn in cases:
        times = [_measure(fn) for _ in range(3)]
        median_us = statistics.median(time_us for time_us, _ in times)
        median_peak = statistics.median(peak for _, peak in times)
        print(f"{label:34} {median_us:10.2f} us/op   {median_peak:10.0f} bytes peak")


if __name__ == "__main__":
    main()
