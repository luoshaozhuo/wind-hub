"""ProtocolPort 跨驱动契约：能力声明、错误类型与未连接语义。

对三个内置驱动（Modbus / ADS / IEC104）统一断言：

- 构造后、connect 前 ``is_open()`` 为 False 且 ``health()`` 不健康，
  且 health 不得触发隐式重连；
- 未声明的能力调用必须抛 ``ProtocolCapabilityError``（显式失败，
  不允许静默降级）；
- ``ProtocolCapabilityError`` 是 ``ProtocolError`` 的子类，调用方可以
  统一按协议错误捕获；
- ``validate_read_many_results`` 拒绝数量不符、乱序或错位的批量结果。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.application import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolCapabilityError,
    ProtocolError,
    ProtocolSample,
    ProtocolWrite,
    Quality,
    WriteOutcome,
    validate_read_many_results,
)
from core.domain import ConnectionEndpoint, PointTable, PointTableId, Protocol
from core.infrastructure.protocol.ads import ADSDriver
from core.infrastructure.protocol.iec104 import IEC104Driver
from core.infrastructure.protocol.modbus import ModbusDriver


def _drivers() -> dict[str, object]:
    return {
        "modbus": ModbusDriver(
            ConnectionEndpoint("192.0.2.10", 502),
            PointTable(PointTableId("pt"), Protocol("modbus"), {}),
            {},
        ),
        "ads": ADSDriver(
            ConnectionEndpoint("192.0.2.20", 801),
            PointTable(PointTableId("pt"), Protocol("ads"), {}),
            {},
        ),
        "iec104": IEC104Driver(
            ConnectionEndpoint("192.0.2.30", 2404),
            PointTable(PointTableId("pt"), Protocol("iec104"), {}),
            {},
        ),
    }


@pytest.fixture(params=["modbus", "ads", "iec104"])
def driver(request: pytest.FixtureRequest) -> object:
    return _drivers()[request.param]


class TestClosedStateSemantics:
    def test_is_open_false_before_connect(self, driver: object) -> None:
        assert driver.is_open() is False  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_health_unhealthy_without_reconnect_side_effect(self, driver: object) -> None:
        health = await driver.health()  # type: ignore[attr-defined]
        assert isinstance(health, ConnectionHealth)
        assert health.healthy is False
        # health 探测不得改变本地传输状态（不做隐式重连）。
        assert driver.is_open() is False  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_close_is_idempotent_before_connect(self, driver: object) -> None:
        await driver.close()  # type: ignore[attr-defined]
        await driver.close()  # type: ignore[attr-defined]
        assert driver.is_open() is False  # type: ignore[attr-defined]


class TestCapabilityDeclaration:
    def test_capabilities_are_declared_subset(self, driver: object) -> None:
        capabilities = driver.capabilities()  # type: ignore[attr-defined]
        assert isinstance(capabilities, frozenset)
        assert capabilities
        assert capabilities <= frozenset(ProtocolCapability)

    @pytest.mark.asyncio
    async def test_undeclared_capabilities_raise_capability_error(self, driver: object) -> None:
        capabilities = driver.capabilities()  # type: ignore[attr-defined]

        async def callback(_samples: tuple[ProtocolSample, ...]) -> None:
            return None

        if ProtocolCapability.SUBSCRIBE not in capabilities:
            with pytest.raises(ProtocolCapabilityError):
                await driver.subscribe(("p",), callback)  # type: ignore[attr-defined]
        if ProtocolCapability.INTERROGATE not in capabilities:
            with pytest.raises(ProtocolCapabilityError):
                await driver.interrogate()  # type: ignore[attr-defined]
        if ProtocolCapability.WRITE_MANY not in capabilities:
            with pytest.raises((ProtocolCapabilityError, NotImplementedError)):
                await driver.write_many(())  # type: ignore[attr-defined]

    def test_capability_error_is_protocol_error(self) -> None:
        assert issubclass(ProtocolCapabilityError, ProtocolError)


class TestReadManyResultContract:
    def _sample(self, point_id: str) -> ProtocolSample:
        return ProtocolSample(
            point_id=point_id,
            value=1.0,
            quality=Quality.GOOD,
            timestamp=datetime.now(UTC),
        )

    def test_accepts_matching_results_with_duplicates(self) -> None:
        samples = [self._sample("b"), self._sample("a"), self._sample("a")]
        validate_read_many_results(["b", "a", "a"], samples)

    def test_rejects_count_mismatch(self) -> None:
        with pytest.raises(ProtocolError, match="2 sample"):
            validate_read_many_results(["a"], [self._sample("a"), self._sample("a")])

    def test_rejects_out_of_order_results(self) -> None:
        with pytest.raises(ProtocolError, match="position 0"):
            validate_read_many_results(["a", "b"], [self._sample("b"), self._sample("a")])

    def test_rejects_misassigned_point(self) -> None:
        with pytest.raises(ProtocolError, match="position 1"):
            validate_read_many_results(["a", "b"], [self._sample("a"), self._sample("c")])


class TestWriteContract:
    def test_write_result_normalizes_point_id(self) -> None:
        from core.application import ProtocolWriteResult

        result = ProtocolWriteResult(point_id=" p ", outcome=WriteOutcome.CONFIRMED_SUCCESS)
        assert result.point_id == "p"
        assert result.message is None
        with pytest.raises(ValueError, match="point_id"):
            ProtocolWriteResult(point_id=" ", outcome=WriteOutcome.CONFIRMED_FAILURE, message="x")

    def test_write_normalizes_point_id(self) -> None:
        assert ProtocolWrite(point=" p ", value=1).point == "p"
