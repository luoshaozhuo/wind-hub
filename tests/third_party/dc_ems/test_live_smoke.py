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


@pytest.mark.asyncio
async def test_gateway_modbus_random_input_changes() -> None:
    """真实 Gateway 端口应暴露动态随机数据，不用内存对象作真值。"""
    host = os.environ.get("DCEMS_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("DCEMS_GATEWAY_PORT", "15221"))
    # 来自 DCEMS 历史契约：风速 F32 CDAB，零基保持寄存器偏移 41503。
    address = 41503
    client = AsyncModbusTcpClient(host, port=port, timeout=3)
    connected = await client.connect()
    assert connected, "Gateway Modbus TCP endpoint unavailable"
    try:
        observations: set[tuple[int, ...]] = set()
        for _ in range(8):
            result = await client.read_holding_registers(address, count=2, device_id=1)
            assert not result.isError(), f"Modbus exception: {result}"
            observations.add(tuple(result.registers))
            await asyncio.sleep(0.4)
        assert len(observations) >= 2, "random source did not update Gateway wind speed"
    finally:
        client.close()
