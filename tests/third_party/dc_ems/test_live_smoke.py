"""可选择执行的 DCEMS Web/Modbus 真实进程烟雾测试。"""

from __future__ import annotations

import asyncio
import os
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from pymodbus.client import AsyncModbusTcpClient


@pytest.fixture(scope="module", autouse=True)
def live_only() -> None:
    if os.environ.get("DCEMS_LIVE") != "1":
        pytest.skip("set DCEMS_LIVE=1 after starting the isolated DCEMS stack")


def test_web_container_serves_application() -> None:
    """Nginx 静态资源必须真实可达；不等同于 Admin 功能已验收。"""
    url = os.environ.get("DCEMS_WEB_URL", "http://127.0.0.1:18080/dc-ems/")
    with urlopen(url, timeout=8) as response:
        document = response.read(512_000).decode("utf-8")
        assert response.status == 200
    assert "<html" in document.lower()
    assert "<script" in document.lower()


def test_web_api_does_not_return_gateway_error() -> None:
    """API 代理不得返回 502/504；登录鉴权及业务功能另行测试。"""
    base = os.environ.get("DCEMS_WEB_URL", "http://127.0.0.1:18080/dc-ems/")
    url = base.split("/dc-ems/")[0] + "/api/"
    try:
        with urlopen(url, timeout=8) as response:
            assert response.status not in {502, 503, 504}
    except HTTPError as exc:
        assert exc.code not in {502, 503, 504}


@pytest.mark.parametrize("device_index", range(10))
@pytest.mark.asyncio
async def test_gateway_modbus_random_input_changes(device_index: int) -> None:
    """真实 Gateway 端口应暴露动态随机数据，不用内存对象作真值。"""
    host = os.environ.get("DCEMS_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_GATEWAY_PORT", "15221")) + device_index
    # 专用烟雾配置：业务点 wind_speed = 1.3.0，F32 ABCD。\n    address = 0
    client = AsyncModbusTcpClient(host, port=port, timeout=3)
    connected = await client.connect()
    assert connected, "Gateway Modbus TCP endpoint unavailable"
    try:
        observations: set[tuple[int, ...]] = set()
        for _ in range(4):
            result = await client.read_input_registers(address, count=2, device_id=1)
            assert not result.isError(), f"Modbus exception: {result}"
            observations.add(tuple(result.registers))
            await asyncio.sleep(0.6)
        assert len(observations) >= 2, (
            f"wind speed frozen at Gateway {device_index + 1}: {observations}"
        )
    finally:
        client.close()



@pytest.mark.asyncio
async def test_station_power_and_primary_frequency_mode_over_gateway() -> None:
    """首台 Gateway 输出站控 P/Q 与调频模式；算法是否消费由独立测试验证。"""
    host = os.environ.get("DCEMS_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_GATEWAY_PORT", "15221"))
    client = AsyncModbusTcpClient(host, port=port, timeout=3)
    assert await client.connect()
    try:
        power_samples: set[tuple[int, ...]] = set()
        for _ in range(4):
            p = await client.read_input_registers(2, count=2, device_id=1)
            q = await client.read_input_registers(4, count=2, device_id=1)
            mode = await client.read_discrete_inputs(0, count=1, device_id=1)
            assert not p.isError()
            assert not q.isError()
            assert not mode.isError()
            assert mode.bits[0] in (True, False)
            power_samples.add(tuple(p.registers))
            await asyncio.sleep(0.6)
        assert len(power_samples) >= 2, "station active power did not change"
    finally:
        client.close()
