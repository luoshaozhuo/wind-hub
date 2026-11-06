"""协议只读诊断 protocol.py 契约。

关键不变量：
- 只执行 connect/health/(read_one|read_many)，绝不写入/订阅/总召；
- 每次诊断使用调用方提供的独立 driver 实例，诊断结束必关闭，关闭失败不覆盖结果；
- 不健康、连接失败、读取超时、能力缺失均转显式结果（FAIL/SKIPPED）；
- benchmark 串行、只读、有上限。

v1_20 起 driver 构造由宿主装配层负责，``check_protocol`` / ``benchmark_protocol``
直接接收已构造的 ProtocolPort；旧的 ``_driver(config, device_id)`` 工厂及其
配置分发测试已随该职责迁出 core（替代覆盖见 tests/cli 的 diagnose 套件）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from core.application.diagnostic import DiagnosticStatus
from core.application.protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
)
from core.domain.sample_value import Quality
from core.infrastructure.diagnostic import protocol


class FakeDriver:
    """记录全部调用的可控 ProtocolPort 双机；写/订阅类方法显式爆炸。"""

    def __init__(
        self,
        *,
        capabilities: frozenset[ProtocolCapability] = frozenset(
            {ProtocolCapability.READ, ProtocolCapability.READ_MANY}
        ),
        healthy: bool = True,
        health_message: str | None = None,
        connect_raises: Exception | None = None,
        read_value: float = 35000.0,
        delay: float = 0.0,
        close_raises: Exception | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._healthy = healthy
        self._health_message = health_message
        self._connect_raises = connect_raises
        self._read_value = read_value
        self._delay = delay
        self._close_raises = close_raises
        self.calls: list[str] = []
        self.closed = False
        self._open = False

    def is_open(self) -> bool:
        return self._open

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return self._capabilities

    async def connect(self) -> None:
        self.calls.append("connect")
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._connect_raises:
            raise self._connect_raises
        self._open = True

    async def health(self) -> ConnectionHealth:
        self.calls.append("health")
        return ConnectionHealth(healthy=self._healthy, message=self._health_message)

    async def read_one(self, point_id: str) -> ProtocolSample:
        self.calls.append(f"read_one:{point_id}")
        if self._delay:
            await asyncio.sleep(self._delay)
        return ProtocolSample(
            point_id=point_id,
            value=self._read_value,
            quality=Quality.GOOD,
            timestamp=datetime.now(UTC),
        )

    async def read_many(self, point_ids: list[str]):
        self.calls.append("read_many:" + ",".join(point_ids))
        return tuple(
            ProtocolSample(
                point_id=pid,
                value=self._read_value,
                quality=Quality.GOOD,
                timestamp=datetime.now(UTC),
            )
            for pid in point_ids
        )

    async def close(self) -> None:
        self.calls.append("close")
        self.closed = True
        self._open = False
        if self._close_raises:
            raise self._close_raises

    # ---- 诊断路径严禁触碰的能力 ----
    async def write(self, *args, **kwargs):
        raise AssertionError("diagnostic must never write")

    async def write_many(self, *args, **kwargs):
        raise AssertionError("diagnostic must never write_many")

    async def subscribe(self, *args, **kwargs):
        raise AssertionError("diagnostic must never subscribe")

    async def interrogate(self, *args, **kwargs):
        raise AssertionError("diagnostic must never interrogate")


async def test_check_pass_connects_healthchecks_and_closes() -> None:
    driver = FakeDriver()

    result = await protocol.check_protocol(driver)

    assert result.status is DiagnosticStatus.PASS
    assert result.findings[0].code == "PROTOCOL_OK"
    assert result.evidence == {"connected": True, "healthy": True}
    assert result.target == "FakeDriver"
    assert driver.calls == ["connect", "health", "close"]
    assert driver.closed


async def test_check_with_point_records_value_without_write() -> None:
    driver = FakeDriver()

    result = await protocol.check_protocol(driver, point_id="grid_freq")

    assert result.status is DiagnosticStatus.PASS
    point = result.evidence["point"]
    assert point["point_id"] == "grid_freq"
    assert point["quality"] == "good"
    assert "T" in point["timestamp"]
    assert driver.calls == ["connect", "health", "read_one:grid_freq", "close"]


async def test_unhealthy_driver_is_fail_and_still_closed() -> None:
    driver = FakeDriver(healthy=False, health_message="protocol stalled")

    result = await protocol.check_protocol(driver)

    assert result.status is DiagnosticStatus.FAIL
    assert "protocol stalled" in result.findings[0].message
    assert driver.closed


async def test_connect_failure_is_fail_and_still_closed() -> None:
    driver = FakeDriver(connect_raises=ConnectionError("refused"))

    result = await protocol.check_protocol(driver)

    assert result.status is DiagnosticStatus.FAIL
    assert "ConnectionError" in result.findings[0].message
    assert driver.closed


async def test_missing_read_capability_is_skipped() -> None:
    driver = FakeDriver(capabilities=frozenset())

    result = await protocol.check_protocol(driver, point_id="p")

    assert result.status is DiagnosticStatus.SKIPPED
    assert result.findings[0].code == "READ_UNSUPPORTED"
    assert driver.closed


async def test_close_failure_degrades_pass_to_unknown() -> None:
    """关闭无法确认时不得虚报 PASS：探测成功也降级为 UNKNOWN 并附 CLOSE_FAILED。"""
    driver = FakeDriver(close_raises=OSError("fd gone"))

    result = await protocol.check_protocol(driver)

    assert result.status is DiagnosticStatus.UNKNOWN
    assert result.findings[0].code == "PROTOCOL_OK"
    assert result.findings[-1].code == "CLOSE_FAILED"


async def test_timeout_aborts_and_closes() -> None:
    driver = FakeDriver(delay=1.0)

    result = await protocol.check_protocol(driver, timeout=0.05)

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "DIAGNOSTIC_TIMEOUT"
    assert driver.closed


# --------------------------------------------------------------------------
# benchmark_protocol
# --------------------------------------------------------------------------


async def test_benchmark_single_point_uses_read_one() -> None:
    driver = FakeDriver()

    result = await protocol.benchmark_protocol(driver, ["p1"], samples=4, interval=0, timeout=5)

    assert result.status is DiagnosticStatus.PASS
    assert result.measurements["successes"] == 4
    assert result.measurements["points"] == 4
    assert driver.calls.count("read_one:p1") == 4
    assert driver.closed


async def test_benchmark_multiple_points_requires_read_many() -> None:
    driver = FakeDriver(capabilities=frozenset({ProtocolCapability.READ}))

    result = await protocol.benchmark_protocol(driver, ["p1", "p2"])

    assert result.status is DiagnosticStatus.FAIL
    assert "ValueError" in result.findings[0].message
    assert driver.closed


async def test_benchmark_single_point_requires_read() -> None:
    driver = FakeDriver(capabilities=frozenset({ProtocolCapability.READ_MANY}))

    result = await protocol.benchmark_protocol(driver, ["p1"])

    assert result.status is DiagnosticStatus.FAIL
    assert "ValueError" in result.findings[0].message
    assert driver.closed


@pytest.mark.parametrize(
    "points",
    [(), ("a", "a"), tuple(str(i) for i in range(201))],
)
async def test_benchmark_point_list_bounds(points) -> None:
    driver = FakeDriver()

    with pytest.raises(ValueError, match="1..200 unique"):
        await protocol.benchmark_protocol(driver, list(points))


async def test_benchmark_read_failure_yields_warning_not_retried() -> None:
    class FlakyDriver(FakeDriver):
        def __init__(self) -> None:
            super().__init__()
            self.n = 0

        async def read_one(self, point_id: str) -> ProtocolSample:
            self.calls.append("read_one")
            self.n += 1
            if self.n == 2:
                raise TimeoutError("slow device")
            return await super().read_one(point_id)

    driver = FlakyDriver()

    result = await protocol.benchmark_protocol(driver, ["p"], samples=3, interval=0)

    assert result.status.value == "WARNING"
    assert result.measurements["failures"] == 1
    assert driver.calls.count("read_one") == 3


async def test_benchmark_multiple_points_uses_read_many() -> None:
    driver = FakeDriver()

    result = await protocol.benchmark_protocol(driver, ["p1", "p2"], samples=3, interval=0)

    assert result.status is DiagnosticStatus.PASS
    assert result.measurements["points"] == 6
    assert driver.calls.count("read_many:p1,p2") == 3
