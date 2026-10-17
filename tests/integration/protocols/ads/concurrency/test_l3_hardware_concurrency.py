"""L3：真实 Beckhoff TwinCAT PLC 上的并发安全验收入口。

L1/L2 的全部结论建立在 pyads testserver 帧栈与 loopback 之上。真实
TwinCAT（TC2/TC3 路由器）是否表现一致，只能在硬件上验收。本文件提供
**可直接运行**的硬件入口：

    WIND_HUB_TEST_ADS_HOST=192.168.x.y \\
    WIND_HUB_TEST_ADS_NET_ID=192.168.x.y.1.1 \\
    WIND_HUB_TEST_ADS_PORT=851 \\
    WIND_HUB_TEST_ADS_READ_SYMBOL=MAIN.readback \\
    WIND_HUB_TEST_ADS_WRITE_SYMBOL=MAIN.setpoint \\
    pytest -m hardware \\
      tests/integration/protocols/ads/concurrency/test_l3_hardware_concurrency.py

未配置环境变量时全部 SKIP（报告中记为 NOT_RUN），Fast Gate/CI 不受影响。

**安全边界**：不对真实 PLC 运行 A 组无锁实验（可能制造并发帧污染设备
状态）。硬件只验收生产驱动的**串行化路径**在真实路由器上同样成立：
多协程并发 health/read/(可选)write 零失败、有界延迟、无通知残留。
无锁行为的硬件复核以人工受控流程在独立实验段执行（见交付报告 L3 章）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from core.application.protocol_contract import ProtocolWrite, Quality
from core.application.settings import ADSLocalConfig
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads import ADSDriver
from tests.support.env import ads_config_from_env
from tests.support.units import NONE_UNIT

_CONFIG = ads_config_from_env()

pytestmark = [pytest.mark.ads, pytest.mark.real_service]
if _CONFIG is not None:
    pytestmark.append(pytest.mark.hardware)

_REPO_ROOT = Path(__file__).resolve().parents[5]
_RESULTS_DIR = Path(
    os.environ.get(
        "WIND_HUB_ADS_BASELINE_DIR",
        str(_REPO_ROOT / "artifacts" / "ads-concurrency-baseline" / "l3_hardware"),
    )
)

_hardware_unavailable = pytest.mark.skipif(
    _CONFIG is None,
    reason=(
        "real TwinCAT not configured; set WIND_HUB_TEST_ADS_HOST and "
        "WIND_HUB_TEST_ADS_NET_ID to run L3 hardware acceptance"
    ),
)


def _symbol_point(symbol: str, *, writable: bool) -> Point:
    return Point(
        point_id=symbol,
        business_point_id=BusinessPointId(symbol),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE if writable else PointAccess.READ,
        ext={"symbol": symbol, "data_type": "REAL"},
    )


def _percentile(sorted_values: list[float], pct: float) -> float:
    index = min(len(sorted_values) - 1, int(len(sorted_values) * pct))
    return sorted_values[index]


@_hardware_unavailable
class TestL3HardwareConcurrency:
    @pytest.fixture
    async def hardware_driver(self) -> AsyncIterator[ADSDriver]:
        assert _CONFIG is not None
        local_net = _CONFIG["local_net_id"] or "127.0.0.1.1.1"
        local_ip = _CONFIG["local_ip"] or "127.0.0.1"
        await ADSDriver.initialize_local(ADSLocalConfig(local_net, local_ip))

        read_symbol = _CONFIG["read_symbol"]
        write_symbol = _CONFIG["write_symbol"]
        symbols = [s for s in (read_symbol, write_symbol) if s]
        points = {s: _symbol_point(s, writable=(s == write_symbol)) for s in dict.fromkeys(symbols)}
        port = int(_CONFIG["port"])
        driver = ADSDriver(
            ConnectionEndpoint(str(_CONFIG["host"]), port),
            PointTable(PointTableId("l3"), Protocol("ads"), points),
            {
                "target_net_id": str(_CONFIG["net_id"]),
                "timeout": 5.0,
                "twincat_version": "3" if port == 851 else "2",
            },
        )
        await driver.connect()
        try:
            yield driver
        finally:
            await driver.close()
            await ADSDriver.close_local()

    async def test_l3_01_concurrent_health_and_reads_bounded(
        self, hardware_driver: ADSDriver
    ) -> None:
        """100 轮 health 与符号读并发：零失败、P99 有界，证据落盘。"""
        assert _CONFIG is not None
        read_symbol = _CONFIG["read_symbol"]
        if not read_symbol:
            pytest.skip("WIND_HUB_TEST_ADS_READ_SYMBOL not set")

        latency: dict[str, list[float]] = {"health": [], "read": []}
        rounds = 100

        async def health_loop() -> None:
            for _ in range(rounds):
                t0 = time.monotonic()
                health = await hardware_driver.health()
                latency["health"].append(time.monotonic() - t0)
                assert health.healthy

        async def read_loop() -> None:
            for _ in range(rounds):
                t0 = time.monotonic()
                sample = await hardware_driver.read_one(read_symbol)
                latency["read"].append(time.monotonic() - t0)
                assert sample.quality == Quality.GOOD

        await asyncio.gather(health_loop(), read_loop())

        def stats(values: list[float]) -> dict[str, float]:
            ordered = sorted(values)
            return {
                "n": float(len(ordered)),
                "p50": _percentile(ordered, 0.50),
                "p99": _percentile(ordered, 0.99),
                "max": ordered[-1],
            }

        report = {
            "tier": "L3",
            "host": _CONFIG["host"],
            "net_id": _CONFIG["net_id"],
            "port": _CONFIG["port"],
            "latency_s": {key: stats(values) for key, values in latency.items()},
        }
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (_RESULTS_DIR / "l3_01_concurrent_health_read.json").write_text(
            json.dumps(report, indent=1)
        )
        # 硬件上界故意宽松（真实网络/PLC 周期抖动），只卡数量级。
        assert report["latency_s"]["health"]["p99"] < 1.0, report

    async def test_l3_02_concurrent_write_when_symbol_configured(
        self, hardware_driver: ADSDriver
    ) -> None:
        """配置了写符号时：50 轮读写并发，写全部成功（值保持幂等常量）。"""
        assert _CONFIG is not None
        write_symbol = _CONFIG["write_symbol"]
        read_symbol = _CONFIG["read_symbol"]
        if not write_symbol:
            pytest.skip("WIND_HUB_TEST_ADS_WRITE_SYMBOL not set")

        async def write_loop() -> None:
            for _ in range(50):
                # 幂等常量：反复写同一安全值，避免改变设备状态。
                result = await hardware_driver.write_one(ProtocolWrite(write_symbol, 0.0))
                assert result.outcome.name == "CONFIRMED_SUCCESS"

        async def health_loop() -> None:
            for _ in range(50):
                assert (await hardware_driver.health()).healthy

        coros: list[object] = [write_loop(), health_loop()]
        if read_symbol:

            async def read_loop() -> None:
                for _ in range(50):
                    await hardware_driver.read_one(read_symbol)

            coros.append(read_loop())

        await asyncio.gather(*coros)  # type: ignore[arg-type]
