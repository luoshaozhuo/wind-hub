"""IEC104 断线后清理旧会话并显式重建。"""

from __future__ import annotations

import pytest

from core.application.errors import ProtocolConnectionError
from tests.protocol.conftest import iec104_driver


@pytest.mark.asyncio
async def test_iec104_disconnect_and_session_recovery(iec104_server) -> None:
    driver = iec104_driver(iec104_server.port)
    await driver.connect()
    try:
        assert (await driver.read_one("power")).value == pytest.approx(1500.5)
        await iec104_server.stop()
        with pytest.raises(ProtocolConnectionError):
            await driver.read_one("power")
        await driver.close()
        await iec104_server.start()
        await driver.connect()
        assert (await driver.read_one("power")).value == pytest.approx(1500.5)
    finally:
        await driver.close()
