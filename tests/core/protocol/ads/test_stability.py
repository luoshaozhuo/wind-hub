"""ADS 编解码重复执行的一致性。"""

from __future__ import annotations

import os
import struct

import pytest

from core.infrastructure.protocol.ads.mapping import ADSPoint


def test_ads_compiled_decoder_repeated_reads() -> None:
    point = ADSPoint(
        point_id="p",
        index_group=0x4020,
        index_offset=24,
        data_type="DINT",
        size=4,
        symbol="MAIN.p",
    )
    raw = struct.pack("<i", -100000)
    rounds = int(os.environ.get("PROTOCOL_STABILITY_ROUNDS", "1000"))
    assert rounds > 0
    for _ in range(rounds):
        assert point.decoder(raw, 0) == -100000


def test_ads_notification_planner_soak() -> None:
    """反复重新构造订阅分块，不得产生错位或数据漂移。"""
    import time

    from core.infrastructure.protocol.ads.codec import compile_notification_blocks

    points = tuple(
        ADSPoint(
            point_id=f"p{i}",
            index_group=0x4020,
            index_offset=4 * i,
            data_type="DINT",
            size=4,
            symbol=f"MAIN.p{i}",
        )
        for i in range(80)
    )
    duration = float(os.environ.get("PROTOCOL_STABILITY_SECONDS", "5"))
    assert duration > 0
    end = time.monotonic() + duration
    rounds = 0
    while time.monotonic() < end:
        blocks = compile_notification_blocks(points, max_bytes=64)
        assert sum(len(block.fields) for block in blocks) == 80
        assert all(block.size <= 64 for block in blocks)
        rounds += 1
    assert rounds > 0


@pytest.mark.asyncio
async def test_real_ads_session_soak() -> None:
    """连接真实 ADS TestServer，持续 Sum Read 与单点读，验证报文解码不串点。"""
    import asyncio
    import time

    from core.application.settings import ADSLocalConfig
    from core.domain import ConnectionEndpoint, PointAccess
    from core.domain.value_objects import DataType
    from core.infrastructure.protocol.ads import ADSDriver
    from scripts.protocol_servers import EndpointPlan, _ADSVirtualServer
    from tests.core.protocol.conftest import make_point, make_table

    point = make_point("power", access=PointAccess.READ, symbol="MAIN.power", data_type="REAL")
    plan = EndpointPlan(
        device_id="ads-soak",
        protocol="ads",
        host="127.0.0.39",
        port=48898,
        options={},
        points=(point,),
        data_types={"power": DataType.FLOAT32},
    )
    server = _ADSVirtualServer(plan)
    driver = ADSDriver(
        ConnectionEndpoint(plan.host, 851),
        make_table("ads", point),
        {"target_net_id": "127.0.0.39.1.1"},
    )
    duration = float(os.environ.get("PROTOCOL_STABILITY_SECONDS", "5"))
    assert duration > 0
    await server.start()
    try:
        await ADSDriver.initialize_local(ADSLocalConfig("127.0.0.1.1.1", "127.0.0.1"))
        try:
            await driver.connect()
            deadline = time.monotonic() + duration
            count = 0
            while time.monotonic() < deadline:
                one = await driver.read_one("power")
                many = await driver.read_many(["power", "power"])
                assert one.point_id == "power"
                assert len(many) == 2
                assert all(sample.point_id == "power" for sample in many)
                count += 1
                await asyncio.sleep(0)
            assert count > 0
        finally:
            await driver.close()
            await ADSDriver.close_local()
    finally:
        await server.stop()
