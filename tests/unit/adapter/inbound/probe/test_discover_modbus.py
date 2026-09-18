"""Unit tests for Modbus 寄存器扫描（``cli/probe/discover.py`` 的 Modbus 路径）。

pymodbus 客户端通过替换模块级 ``_create_modbus_client`` 来 mock——无需真实
从站。FakeClient 按「可读地址集合」模拟从站行为。
"""

from __future__ import annotations

import pytest

from wind_hub.adapter.inbound.cli.probe import discover
from wind_hub.config.schema import DeviceConfig
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ProtocolError


def _modbus_device() -> DeviceConfig:
    return DeviceConfig(
        device_id="wtg-002",
        protocol="modbus",
        endpoint=Endpoint(host="10.0.2.1", port=502, extensions={"unit_id": 1}),
    )


class _FakeResponse:
    def __init__(self, error: bool) -> None:
        self._error = error

    # 刻意与 pymodbus ModbusResponse.isError() 的 camelCase API 同名：
    # 驱动按 pymodbus 原名调用，fake 必须同名才能命中。
    def isError(self) -> bool:  # noqa: N802
        return self._error


class _FakeClient:
    """模拟 pymodbus 异步客户端：只有 ``readable`` 集合内的地址读得通。"""

    def __init__(self, readable: set[int], connect_ok: bool = True) -> None:
        self.readable = readable
        self.connect_ok = connect_ok
        self.closed = False

    async def connect(self) -> bool:
        return self.connect_ok

    def close(self) -> None:
        self.closed = True

    async def read_holding_registers(
        self, address: int, *, count: int, device_id: int
    ) -> _FakeResponse:
        assert count == 1
        return _FakeResponse(error=address not in self.readable)


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr(discover, "_create_modbus_client", lambda _cfg: client)


# ---------------------------------------------------------------------------
# 范围解析
# ---------------------------------------------------------------------------


def test_parse_scan_ranges_single() -> None:
    assert discover.parse_scan_ranges("0-1000") == [(0, 1000)]


def test_parse_scan_ranges_multiple_and_single_address() -> None:
    assert discover.parse_scan_ranges("0-100, 200-300,500") == [(0, 100), (200, 300), (500, 500)]


@pytest.mark.parametrize(
    "spec",
    ["", "abc", "1-x", "100-50", "-1-10", "0-70000", "0..100", "0-1,"],
)
def test_parse_scan_ranges_invalid(spec: str) -> None:
    with pytest.raises(ValueError):
        discover.parse_scan_ranges(spec)


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


async def test_discover_modbus_records_only_readable_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(readable={0, 2})
    _patch_client(monkeypatch, client)

    points = await discover.discover_modbus(_modbus_device(), [(0, 4)])

    assert [p.address for p in points] == [
        {"register_type": "holding", "address": 0},
        {"register_type": "holding", "address": 2},
    ]
    assert all(p.data_type == "int16" and p.size == 2 for p in points)
    assert all(p.comment == "扫描结果，需人工确认" for p in points)
    assert [p.symbol for p in points] == ["holding[0]", "holding[2]"]
    assert client.closed is True


async def test_discover_modbus_transport_error_skips_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FlakyClient(_FakeClient):
        async def read_holding_registers(
            self, address: int, *, count: int, device_id: int
        ) -> _FakeResponse:
            if address == 1:
                raise OSError("connection reset")
            return await super().read_holding_registers(address, count=count, device_id=device_id)

    _patch_client(monkeypatch, _FlakyClient(readable={0, 1, 2}))
    points = await discover.discover_modbus(_modbus_device(), [(0, 2)])
    # 地址 1 传输异常 → 按不可读跳过
    assert [p.address["address"] for p in points] == [0, 2]


async def test_discover_modbus_connect_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, _FakeClient(readable=set(), connect_ok=False))
    with pytest.raises(ProtocolError, match="cannot connect"):
        await discover.discover_modbus(_modbus_device(), [(0, 10)])
