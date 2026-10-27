"""Modbus 公共协议接口与未实现能力的契约。"""

from __future__ import annotations

import pytest

from core.application.errors import ProtocolCapabilityError
from core.application.protocol_contract import (
    InterrogationRequest,
    ProtocolCapability,
    ProtocolWrite,
)
from tests.protocol.conftest import modbus_driver


async def _callback(samples: object) -> None:
    del samples


@pytest.mark.asyncio
async def test_modbus_capabilities_and_unavailable_operations() -> None:
    driver = modbus_driver(15020)
    assert driver.capabilities() == frozenset(
        {ProtocolCapability.READ, ProtocolCapability.READ_MANY, ProtocolCapability.WRITE}
    )
    assert await driver.read_many([]) == ()
    with pytest.raises(ProtocolCapabilityError):
        await driver.write_many([ProtocolWrite("command", 3.0)])
    with pytest.raises(ProtocolCapabilityError):
        driver.subscribe(["speed"], _callback)
    with pytest.raises(ProtocolCapabilityError):
        await driver.listen(["speed"], _callback)
    with pytest.raises(ProtocolCapabilityError):
        await driver.interrogate(InterrogationRequest())


@pytest.mark.asyncio
async def test_modbus_unsent_write_is_confirmed_failure() -> None:
    """会话尚未建立时，不得把未发送命令归类为 UNKNOWN。"""
    from core.application.protocol_contract import WriteOutcome

    driver = modbus_driver(15020)
    result = await driver.write_one(ProtocolWrite("command", 1.0))
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    assert not driver.is_open()
