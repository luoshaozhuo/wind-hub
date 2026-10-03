"""ADSProbe 单元测试——地址解析、验证读取与连接生命周期。

pyads 的真实 PLC 连接在单元层以 ``FakeConnection`` 替代（monkeypatch
``pyads.Connection``）；``PLCTYPE_*`` 使用真实 pyads ctypes 类型，保证
``ctypes.sizeof`` 与类型映射路径与生产一致。真实 TwinCAT 环境下的行为由
``tests/integration/protocols/ads/test_commander_ads_real.py`` 验收。
"""

from __future__ import annotations

import ctypes

import pyads
import pytest

from wind_hub_core.protocol.ads.probe import ADSProbe
from wind_hub_core.validation.models import (
    AddressResolution,
    DeviceProbeTarget,
    PointProbeSpec,
)


class FakeSymbol:
    """pyads get_symbol 返回值的最小替身。"""

    def __init__(
        self,
        index_group: object,
        index_offset: object,
        plc_type: object = None,
        symbol_type: str = "",
    ) -> None:
        self.index_group = index_group
        self.index_offset = index_offset
        self.plc_type = plc_type
        self.symbol_type = symbol_type


class FakeConnection:
    """pyads.Connection 替身——按类属性脚本化行为并记录调用。"""

    instances: list[FakeConnection] = []
    open_error: Exception | None = None
    open_leaves_closed = False
    symbols: dict[str, object] = {}
    read_values: dict[tuple[int, int], object] = {}
    get_symbol_calls: list[str] = []

    def __init__(self, net_id: str | None, port: int, host: str) -> None:
        self.net_id = net_id
        self.port = port
        self.host = host
        self.is_open = False
        self.timeout_ms: int | None = None
        self.open_calls = 0
        self.close_calls = 0
        self.read_calls: list[tuple[int, int, object]] = []
        FakeConnection.instances.append(self)

    def set_timeout(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms

    def open(self) -> None:
        self.open_calls += 1
        if FakeConnection.open_error is not None:
            raise FakeConnection.open_error
        self.is_open = not FakeConnection.open_leaves_closed

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False

    def get_symbol(self, name: str) -> FakeSymbol:
        FakeConnection.get_symbol_calls.append(name)
        symbol = FakeConnection.symbols[name]
        if isinstance(symbol, Exception):
            raise symbol
        assert isinstance(symbol, FakeSymbol)
        return symbol

    def read(self, index_group: int, index_offset: int, plc_type: object) -> object:
        self.read_calls.append((index_group, index_offset, plc_type))
        value = FakeConnection.read_values[(index_group, index_offset)]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def fake_pyads(monkeypatch: pytest.MonkeyPatch) -> type[FakeConnection]:
    FakeConnection.instances = []
    FakeConnection.open_error = None
    FakeConnection.open_leaves_closed = False
    FakeConnection.symbols = {}
    FakeConnection.read_values = {}
    FakeConnection.get_symbol_calls = []
    monkeypatch.setattr(pyads, "Connection", FakeConnection)
    return FakeConnection


def _target(**options: object) -> DeviceProbeTarget:
    return DeviceProbeTarget(device_id="plc-1", host="192.0.2.10", options=dict(options))


def _point(
    point_id: str = "p1",
    data_type: str = "float32",
    **address: object,
) -> PointProbeSpec:
    return PointProbeSpec(point_id=point_id, data_type=data_type, address=dict(address))


class TestConnect:
    async def test_connect_uses_target_options(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(
            _target(target_net_id="1.2.3.4.1.1", target_port=851, timeout=2.5)
        )
        await probe.connect()
        conn = fake_pyads.instances[0]
        assert conn.net_id == "1.2.3.4.1.1"
        assert conn.port == 851
        assert conn.host == "192.0.2.10"
        assert conn.timeout_ms == 2500
        assert probe.connected

    async def test_connect_default_port_by_twincat_version(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe_tc2 = ADSProbe(_target())
        await probe_tc2.connect()
        assert fake_pyads.instances[0].port == 801

        probe_tc3 = ADSProbe(_target(twincat_version="3"))
        await probe_tc3.connect()
        assert fake_pyads.instances[1].port == 802

    async def test_connect_is_idempotent(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        await probe.connect()
        await probe.connect()
        assert len(fake_pyads.instances) == 1
        assert fake_pyads.instances[0].open_calls == 1

    async def test_connect_failure_closes_connection_and_propagates(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        fake_pyads.open_error = OSError("route unreachable")
        probe = ADSProbe(_target())
        with pytest.raises(OSError, match="route unreachable"):
            await probe.connect()
        conn = fake_pyads.instances[0]
        assert conn.close_calls == 1
        assert not probe.connected

    async def test_connect_raises_when_connection_not_open(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        fake_pyads.open_leaves_closed = True
        probe = ADSProbe(_target())
        with pytest.raises(ConnectionError, match="not open"):
            await probe.connect()
        assert fake_pyads.instances[0].close_calls == 1
        assert not probe.connected


class TestResolve:
    async def test_symbol_only_resolve_queries_plc(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        fake_pyads.symbols["MAIN.speed"] = FakeSymbol(
            16448, 100, pyads.PLCTYPE_REAL, "REAL"
        )
        probe = ADSProbe(_target())
        await probe.connect()
        resolved = await probe.resolve_points([_point(symbol="MAIN.speed")])
        item = resolved["p1"]
        assert item.symbol == "MAIN.speed"
        assert item.index_group == 16448
        assert item.index_offset == 100
        assert item.size == ctypes.sizeof(pyads.PLCTYPE_REAL)
        assert item.protocol_type == "REAL"

    async def test_index_only_resolve_uses_configured_address(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        await probe.connect()
        resolved = await probe.resolve_points(
            [_point(index_group=16449, index_offset=200)]
        )
        item = resolved["p1"]
        assert item.symbol is None
        assert item.index_group == 16449
        assert item.index_offset == 200
        assert item.protocol_type == "float32"
        # index-only 路径不访问 PLC。
        assert fake_pyads.get_symbol_calls == []

    async def test_symbol_wins_over_configured_index(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        """symbol + index 同时配置时，以 PLC symbol 解析结果为准。"""
        fake_pyads.symbols["MAIN.power"] = FakeSymbol(
            16448, 300, pyads.PLCTYPE_REAL, "REAL"
        )
        probe = ADSProbe(_target())
        await probe.connect()
        resolved = await probe.resolve_points(
            [_point(symbol="MAIN.power", index_group=1, index_offset=2)]
        )
        item = resolved["p1"]
        assert (item.index_group, item.index_offset) == (16448, 300)

    async def test_resolve_uses_session_cache(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        fake_pyads.symbols["MAIN.speed"] = FakeSymbol(16448, 100)
        probe = ADSProbe(_target())
        await probe.connect()
        point = _point(symbol="MAIN.speed")
        first = await probe.resolve_points([point])
        second = await probe.resolve_points([point])
        assert fake_pyads.get_symbol_calls == ["MAIN.speed"]
        assert first["p1"] is second["p1"]

    async def test_resolve_requires_connection(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        with pytest.raises(ConnectionError, match="not connected"):
            await probe.resolve_points([_point(index_group=1, index_offset=2)])

    async def test_resolve_rejects_address_without_symbol_and_index(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        await probe.connect()
        with pytest.raises(ValueError, match="neither symbol nor index"):
            await probe.resolve_points([_point()])

    async def test_resolve_rejects_partial_index_address(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        await probe.connect()
        with pytest.raises(ValueError, match="neither symbol nor index"):
            await probe.resolve_points([_point(index_group=16448)])

    async def test_symbol_not_found_propagates(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        fake_pyads.symbols["MAIN.missing"] = RuntimeError("symbol not found")
        probe = ADSProbe(_target())
        await probe.connect()
        with pytest.raises(RuntimeError, match="symbol not found"):
            await probe.resolve_points([_point(symbol="MAIN.missing")])

    async def test_symbol_with_invalid_index_address_rejected(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        fake_pyads.symbols["MAIN.broken"] = FakeSymbol(None, 100)
        probe = ADSProbe(_target())
        await probe.connect()
        with pytest.raises(ValueError, match="invalid index address"):
            await probe.resolve_points([_point(symbol="MAIN.broken")])


class TestReadValue:
    async def test_read_uses_resolved_index_address(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        fake_pyads.symbols["MAIN.speed"] = FakeSymbol(
            16448, 100, pyads.PLCTYPE_REAL, "REAL"
        )
        fake_pyads.read_values[(16448, 100)] = 12.5
        probe = ADSProbe(_target())
        await probe.connect()
        point = _point(symbol="MAIN.speed")
        resolved = (await probe.resolve_points([point]))["p1"]
        value = await probe.read_value(point, resolved)
        assert value == 12.5
        conn = fake_pyads.instances[0]
        assert conn.read_calls == [(16448, 100, pyads.PLCTYPE_REAL)]

    async def test_read_requires_connection(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        resolved = AddressResolution(point_id="p1", symbol=None, index_group=1, index_offset=2)
        with pytest.raises(ConnectionError, match="not connected"):
            await probe.read_value(_point(), resolved)

    async def test_read_rejects_unresolved_address(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        await probe.connect()
        resolved = AddressResolution(point_id="p1", symbol="MAIN.x")
        with pytest.raises(ValueError, match="no resolved index address"):
            await probe.read_value(_point(), resolved)

    async def test_read_rejects_unsupported_data_type(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        await probe.connect()
        resolved = AddressResolution(point_id="p1", symbol=None, index_group=1, index_offset=2)
        with pytest.raises(ValueError, match="unsupported ADS data_type"):
            await probe.read_value(_point(data_type="complex128"), resolved)


class TestVerifyRead:
    async def test_verify_read_collects_only_successful_points(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        fake_pyads.read_values[(1, 10)] = 1.0
        fake_pyads.read_values[(1, 20)] = RuntimeError("read error")
        points = [
            _point("ok", index_group=1, index_offset=10),
            _point("bad", index_group=1, index_offset=20),
            _point("unresolved", index_group=1, index_offset=30),
        ]
        resolutions = {
            "ok": AddressResolution(point_id="ok", symbol=None, index_group=1, index_offset=10),
            "bad": AddressResolution(point_id="bad", symbol=None, index_group=1, index_offset=20),
            # unresolved 无 resolution——必须被跳过而不是报错。
        }
        probe = ADSProbe(_target())
        await probe.connect()
        readable = await probe.verify_read(points, resolutions)
        assert readable == {"ok"}

    async def test_verify_read_requires_connection(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        with pytest.raises(ConnectionError, match="not connected"):
            await probe.verify_read([], {})


class TestClose:
    async def test_close_is_idempotent(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        await probe.connect()
        await probe.close()
        await probe.close()
        assert fake_pyads.instances[0].close_calls == 1
        assert not probe.connected

    async def test_close_without_connect_is_noop(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        await probe.close()
        assert fake_pyads.instances == []

    async def test_operations_after_close_require_reconnect(
        self, fake_pyads: type[FakeConnection]
    ) -> None:
        probe = ADSProbe(_target())
        await probe.connect()
        await probe.close()
        with pytest.raises(ConnectionError, match="not connected"):
            await probe.resolve_points([_point(index_group=1, index_offset=2)])
