"""Modbus 公共协议接口与未实现能力的契约。"""

from __future__ import annotations

import pytest

from core.application.errors import ProtocolCapabilityError
from core.application.protocol_contract import InterrogationRequest, ProtocolCapability, ProtocolWrite
from tests.protocol.conftest import modbus_driver


async def _callback(samples: object) -> None:
    del samples


@pytest.mark.asyncio
async def test_modbus_capabilities_and_unavailable_operations() -> None:
    driver = modbus_driver(15020)
    assert driver.capabilities() == frozenset({
        ProtocolCapability.READ, ProtocolCapability.READ_MANY, ProtocolCapability.WRITE
    })
    assert await driver.read_many([]) == ()
    with pytest.raises(ProtocolCapabilityError):
        await driver.write_many([ProtocolWrite("command", 3.0)])
    with pytest.raises(ProtocolCapabilityError):
        driver.subscribe(["speed"], _callback)
    with pytest.raises(ProtocolCapabilityError):
        await driver.listen(["speed"], _callback)
    with pytest.raises(ProtocolCapabilityError):
        await driver.interrogate(InterrogationRequest())
