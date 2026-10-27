"""使用现有协议 Server 的真实 ADS/AMS TCP 互操作验收。"""

from __future__ import annotations

import pytest

from core.application.protocol_contract import ProtocolWrite, WriteOutcome
from core.application.settings import ADSLocalConfig
from core.domain import ConnectionEndpoint, PointAccess
from core.domain.value_objects import DataType
from core.infrastructure.protocol.ads import ADSDriver
from scripts.protocol_servers import EndpointPlan, _ADSVirtualServer
from tests.protocol.conftest import make_point, make_table


@pytest.mark.asyncio
async def test_real_ads_server_single_read_write_and_batch() -> None:
    point = make_point(
        "power",
        access=PointAccess.READ_WRITE,
        symbol="MAIN.power",
        data_type="REAL",
    )
    plan = EndpointPlan(
        device_id="ads-adapter-test",
        protocol="ads",
        host="127.0.0.37",
        port=48898,
        options={},
        points=(point,),
        data_types={"power": DataType.FLOAT32},
    )
    server = _ADSVirtualServer(plan)
    driver = ADSDriver(
        ConnectionEndpoint(plan.host, 851),
        make_table("ads", point),
        {"target_net_id": "127.0.0.37.1.1"},
    )
    await server.start()
    try:
        await ADSDriver.initialize_local(ADSLocalConfig("127.0.0.1.1.1", "127.0.0.1"))
        try:
            await driver.connect()
            sample = await driver.read_one("power")
            assert sample.point_id == "power"
            samples = await driver.read_many(["power", "power"])
            assert [item.point_id for item in samples] == ["power", "power"]
            result = await driver.write_one(ProtocolWrite("power", 15.0))
            assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
            updated = await driver.read_one("power")
            assert updated.value == pytest.approx(15.0)
        finally:
            await driver.close()
            await ADSDriver.close_local()
    finally:
        await server.stop()
