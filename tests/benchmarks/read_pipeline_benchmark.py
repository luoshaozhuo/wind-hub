"""无网络 Modbus 完整读取微基准。

比较相同协议响应条件下 read_raw（直接点值）与 read（ProtocolSample 包装），
分别测量固定选点及变化选点。结果是本机 Python / Mock 开销，不包含网络时延。

运行：PYTHONPATH=src python tests/benchmarks/read_pipeline_benchmark.py
"""

from __future__ import annotations

import asyncio
import gc
import statistics
import time
import tracemalloc
from collections.abc import Awaitable, Callable

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
    """构建完全本地化的读点配置，避免基准依赖其他脚本。"""
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



class FakeModbusResponse:
    def __init__(self, registers: list[int]) -> None:
        self.registers = registers

    def isError(self) -> bool:  # noqa: N802 — pymodbus compatibility
        return False


class FakeModbusClient:
    """按地址生成确定性响应，避免将网络等待计入结果。"""

    async def read_holding_registers(
        self,
        start: int,
        *,
        count: int,
        device_id: int,
    ) -> FakeModbusResponse:
        del device_id
        return FakeModbusResponse([(start + i) % 65536 for i in range(count)])


async def _measure(
    operation: Callable[[], Awaitable[object]],
    iterations: int,
) -> tuple[float, float, int]:
    for _ in range(100):
        await operation()
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter_ns()
    for _ in range(iterations):
        await operation()
    elapsed = time.perf_counter_ns() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    seconds = elapsed / 1_000_000_000
    return seconds * 1_000_000 / iterations, iterations / seconds, peak


async def main() -> None:
    driver = _driver()
    driver._client = FakeModbusClient()
    driver._connected = True
    fixed = tuple(f"p{i}" for i in range(64))
    dynamic = tuple(
        tuple(f"p{(index * 17 + offset) % 256}" for offset in range(32))
        for index in range(64)
    )

    async def fixed_raw() -> object:
        return await driver.read_raw(fixed)

    async def fixed_dto() -> object:
        return await driver.read(fixed)

    index = 0

    async def dynamic_raw() -> object:
        nonlocal index
        points = dynamic[index % len(dynamic)]
        index += 1
        return await driver.read_raw(points)

    async def dynamic_dto() -> object:
        nonlocal index
        points = dynamic[index % len(dynamic)]
        index += 1
        return await driver.read(points)

    print("Scenario                   us/op      ops/s   peak bytes")
    for name, operation in (
        ("fixed / raw", fixed_raw),
        ("fixed / DTO", fixed_dto),
        ("dynamic / raw", dynamic_raw),
        ("dynamic / DTO", dynamic_dto),
    ):
        samples = [await _measure(operation, 1000) for _ in range(3)]
        elapsed, throughput, peak = (statistics.median(x) for x in zip(*samples, strict=True))
        print(f"{name:26} {elapsed:8.2f} {throughput:10.1f} {peak:12.0f}")


if __name__ == "__main__":
    asyncio.run(main())
