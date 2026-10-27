"""IEC104 公共协议接口与不支持的原生批量操作。"""

from __future__ import annotations

import pytest

from core.application.errors import ProtocolCapabilityError
from core.application.protocol_contract import ProtocolCapability, ProtocolWrite
from tests.protocol.conftest import iec104_driver


def test_iec104_capability_declarations() -> None:
    driver = iec104_driver(12404)
    assert driver.capabilities() == frozenset({
        ProtocolCapability.READ, ProtocolCapability.WRITE,
        ProtocolCapability.INTERROGATE, ProtocolCapability.LISTEN,
    })


@pytest.mark.asyncio
async def test_iec104_batch_methods_reject_without_network() -> None:
    driver = iec104_driver(12404)
    with pytest.raises(ProtocolCapabilityError):
        await driver.read_many(["power"])
    with pytest.raises(ProtocolCapabilityError):
        await driver.write_many([ProtocolWrite("switch", True)])
