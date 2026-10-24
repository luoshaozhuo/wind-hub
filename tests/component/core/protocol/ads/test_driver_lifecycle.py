"""ADS 单 Driver 生命周期保护：路由独占由设备注册层保证。"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from core.application.errors import ProtocolError
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads.driver import ADSDriver
from tests.support.units import NONE_UNIT

pytestmark = [pytest.mark.ads]


def _driver() -> ADSDriver:
    point = Point(
        point_id="p",
        business_point_id=BusinessPointId("p"),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": "DINT", "index_group": 0x4020, "index_offset": 0},
    )
    return ADSDriver(
        ConnectionEndpoint("192.0.2.10", 851),
        PointTable(PointTableId("ads-life"), Protocol("ads"), {"p": point}),
        {"target_net_id": "192.0.2.10.1.1", "timeout": 2.0},
    )


async def test_driver_rejects_reconnect_while_old_close_pending() -> None:
    driver = _driver()
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    driver._pending_close = future
    connection = MagicMock()
    driver._new_connection = lambda: connection  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="not confirmed"):
        await driver.connect()
    connection.open.assert_not_called()
    future.set_result(True)


async def test_driver_rejects_reconnect_after_failed_close() -> None:
    driver = _driver()
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    future.set_result(False)
    driver._pending_close = future
    with pytest.raises(ProtocolError, match="failed"):
        await driver.connect()


async def test_driver_rejects_reconnect_when_close_uncertain() -> None:
    driver = _driver()
    driver._unsafe_close = True
    with pytest.raises(ProtocolError, match="not confirmed"):
        await driver.connect()


async def test_local_close_rejects_uncertain_driver() -> None:
    driver = _driver()
    driver._unsafe_close = True
    with pytest.raises(ProtocolError, match="sessions are active"):
        await ADSDriver.close_local()


async def test_failed_close_blocks_replacement_driver_with_same_target() -> None:
    """An abandoned close must block a new Driver even after old Driver removal."""
    import core.infrastructure.protocol.ads.driver as driver_module

    old = _driver()
    replacement = _driver()
    old._unsafe_close = True
    driver_module._UNSAFE_TARGETS.add(old._route_key)

    connection = MagicMock()
    replacement._new_connection = lambda: connection  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="unconfirmed prior close"):
        await replacement.connect()
    connection.open.assert_not_called()


async def test_failed_close_blocks_local_port_teardown() -> None:
    """The unsafe target registry outlives individual Driver objects."""
    import core.infrastructure.protocol.ads.driver as driver_module

    driver_module._UNSAFE_TARGETS.add("192.0.2.10.1.1")
    with pytest.raises(ProtocolError, match="sessions are active"):
        await ADSDriver.close_local()
