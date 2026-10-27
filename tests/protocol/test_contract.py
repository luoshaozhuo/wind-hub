"""三个协议 Adapter 的公共 ProtocolPort 契约。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.application.errors import ProtocolCapabilityError, ProtocolError
from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WriteOutcome,
    validate_read_many_results,
)
from core.domain import ConnectionEndpoint, PointTable
from core.infrastructure.protocol.ads import ADSDriver
from core.infrastructure.protocol.iec104 import IEC104Driver
from core.infrastructure.protocol.modbus import ModbusDriver
from tests.protocol.conftest import make_table


@pytest.fixture(params=["ads", "iec104", "modbus"])
def driver(request: pytest.FixtureRequest):
    family = request.param
    table: PointTable = make_table(family)
    endpoint = ConnectionEndpoint("192.0.2.20", 801 if family == "ads" else 2404 if family == "iec104" else 502)
    if family == "ads":
        return ADSDriver(endpoint, table, {"target_net_id": "192.0.2.20.1.1"})
    if family == "iec104":
        return IEC104Driver(endpoint, table, {})
    return ModbusDriver(endpoint, table, {})


@pytest.mark.asyncio
async def test_constructed_driver_is_closed_and_health_is_passive(driver) -> None:
    assert not driver.is_open()
    health = await driver.health()
    assert health.healthy is False
    assert not driver.is_open()
    await driver.close()
    await driver.close()


def test_capabilities_subset_of_declared_protocol_enum(driver) -> None:
    assert driver.capabilities()
    assert driver.capabilities() <= frozenset(ProtocolCapability)


@pytest.mark.asyncio
async def test_unavailable_batch_operations_reject(driver) -> None:
    if ProtocolCapability.WRITE_MANY not in driver.capabilities():
        with pytest.raises(ProtocolCapabilityError):
            await driver.write_many([ProtocolWrite("unknown", 1)])
    if ProtocolCapability.READ_MANY not in driver.capabilities():
        with pytest.raises(ProtocolCapabilityError):
            await driver.read_many(["unknown"])


def _sample(point_id: str) -> ProtocolSample:
    return ProtocolSample(point_id=point_id, value=1.0, timestamp=datetime.now(UTC))


def test_read_many_contract_preserves_requested_position_and_duplicates() -> None:
    validate_read_many_results(["a", "b", "a"], [_sample("a"), _sample("b"), _sample("a")])
    with pytest.raises(ProtocolError, match="position"):
        validate_read_many_results(["a", "b"], [_sample("b"), _sample("a")])
    with pytest.raises(ProtocolError, match="returned"):
        validate_read_many_results(["a"], [_sample("a"), _sample("b")])


def test_write_outcomes_and_point_identity_are_explicit() -> None:
    assert ProtocolWrite(" p ", 1).point == "p"
    result = ProtocolWriteResult(" p ", WriteOutcome.UNKNOWN)
    assert result.point_id == "p"
    assert result.outcome is WriteOutcome.UNKNOWN
    assert Quality.GOOD.value == "good"
