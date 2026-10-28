"""IEC104 故障注入：断线及旧 Session 应答隔离。"""

from __future__ import annotations

import pytest

from core.application.errors import ProtocolConnectionError
from tests.core.protocol.conftest import iec104_driver


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


@pytest.mark.asyncio
async def test_close_idempotent_and_old_receive_noop(iec104_server) -> None:
    driver = iec104_driver(iec104_server.port)
    await driver.connect()
    old_session = driver._session
    assert old_session is not None
    await driver.close()
    await driver.close()
    assert not driver.is_open()
    driver._dispatch(old_session, [])
    assert not driver.is_open()


@pytest.mark.asyncio
async def test_wrong_cot_and_wrong_ca_cannot_complete_active_read() -> None:
    """IEC104 read 只能由对应 CA/IOA 且 COT=5 的真实数据报文完成。"""
    import asyncio
    from typing import Any, cast

    from hat.drivers.iec101 import common as iec101

    from core.infrastructure.protocol.iec104.driver import _Read
    from core.infrastructure.protocol.iec104.session import _IEC104Session
    from tests.support.iec104_fake import floating_data_msg

    driver = iec104_driver(12404)
    session = cast(_IEC104Session, cast(Any, object()))
    driver._session = session
    future = asyncio.get_running_loop().create_future()
    driver._read[(1, 100)] = _Read(driver._by_id["power"], future)

    driver._dispatch(session, [floating_data_msg(1, 100, 20.0)])
    assert not future.done(), "spontaneous COT must not complete a pending read"

    driver._dispatch(
        session,
        [floating_data_msg(2, 100, 21.0, cause=iec101.DataResCause.REQUEST)],
    )
    assert not future.done(), "wrong CA must not complete a pending read"

    driver._dispatch(
        session,
        [floating_data_msg(1, 100, 22.0, cause=iec101.DataResCause.REQUEST)],
    )
    assert future.done()
    assert future.result().value == pytest.approx(22.0)
