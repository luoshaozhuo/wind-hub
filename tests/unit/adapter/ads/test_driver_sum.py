"""Unit tests for ADS Sum batching and sequential read dispatch.

Covers the ``read_mode`` dispatch: symbol-addressed Sum reads (batching by
``max_subs_per_sum``, per-symbol failure → ``BAD``, overall failure →
``ProtocolError``) and the ``NotImplementedError`` guard for index/offset
addressing under Sum mode.

``pyads.Connection`` is replaced by :class:`FakeConnection`; no real ADS server
is involved.
"""

from __future__ import annotations

import pytest

from wind_hub.adapter.outbound.protocol.ads.driver import ADSDriver
from wind_hub.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub.domain.model.errors import ProtocolError
from wind_hub.domain.model.point import PointRef, Quality


def _make_device_config(read_mode: str = "sum", **extensions: object) -> DeviceConfig:
    return DeviceConfig(
        device_id="test-dev",
        protocol="ads",
        endpoint=Endpoint(
            host="192.168.0.100",
            port=48898,
            extensions=dict(extensions),
        ),
        read_mode=read_mode,
    )


def _make_symbol_point(point_id: str, symbol: str, data_type: str = "float32") -> PointConfig:
    return PointConfig(
        point_id=point_id,
        device_id="test-dev",
        address=PointAddress(symbol=symbol),
        data_type=data_type,
    )


def _make_index_point(point_id: str) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        device_id="test-dev",
        address=PointAddress(index_group=0x4020, index_offset=0),
        data_type="float32",
    )


class FakeConnection:
    """Synchronous stand-in for ``pyads.Connection`` with a symbol table."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.is_open = True
        self.symbol_values: dict[str, object] = {}
        self.read_list_calls: list[list[str]] = []

    def set_timeout(self, ms: int) -> None:
        pass

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read_list_by_name(self, names: list[str]) -> dict[str, object]:
        self.read_list_calls.append(list(names))
        return {n: self.symbol_values[n] for n in names if n in self.symbol_values}

    def read(self, index_group: int, index_offset: int, plc_datatype: object) -> object:
        return None


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyads.Connection", FakeConnection)


class TestReadSum:
    async def test_read_sum_reads_symbols(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping(
            [
                _make_symbol_point("rotor.speed", "MAIN.rotorSpeed"),
                _make_symbol_point("gen.power", "MAIN.genPower"),
            ]
        )
        driver._connection = FakeConnection()
        driver._connected = True
        driver._connection.symbol_values = {
            "MAIN.rotorSpeed": 1200.5,
            "MAIN.genPower": 800.0,
        }

        values = await driver.read(
            [
                PointRef(device_id="test-dev", point_id="rotor.speed"),
                PointRef(device_id="test-dev", point_id="gen.power"),
            ]
        )

        assert [v.value for v in values] == [1200.5, 800.0]
        assert all(v.quality == Quality.GOOD for v in values)

    async def test_read_sum_batches_by_max_subs_per_sum(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config(max_subs_per_sum=2))
        driver.set_points_mapping(
            [
                _make_symbol_point("a", "MAIN.a"),
                _make_symbol_point("b", "MAIN.b"),
                _make_symbol_point("c", "MAIN.c"),
            ]
        )
        driver._connection = FakeConnection()
        driver._connected = True
        driver._connection.symbol_values = {"MAIN.a": 1, "MAIN.b": 2, "MAIN.c": 3}

        await driver.read(
            [
                PointRef(device_id="test-dev", point_id="a"),
                PointRef(device_id="test-dev", point_id="b"),
                PointRef(device_id="test-dev", point_id="c"),
            ]
        )

        # 3 points, batch cap 2 → two Sum commands of size 2 and 1.
        assert driver._connection.read_list_calls == [["MAIN.a", "MAIN.b"], ["MAIN.c"]]

    async def test_read_sum_missing_symbol_is_bad(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping(
            [
                _make_symbol_point("ok", "MAIN.ok"),
                _make_symbol_point("broken", "MAIN.broken"),
            ]
        )
        driver._connection = FakeConnection()
        driver._connected = True
        driver._connection.symbol_values = {"MAIN.ok": 7.5}  # MAIN.broken absent

        values = await driver.read(
            [
                PointRef(device_id="test-dev", point_id="ok"),
                PointRef(device_id="test-dev", point_id="broken"),
            ]
        )

        assert values[0].quality == Quality.GOOD
        assert values[0].value == 7.5
        assert values[1].quality == Quality.BAD
        assert values[1].value is None

    async def test_read_sum_unknown_point_is_bad(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config())
        driver._connection = FakeConnection()
        driver._connected = True

        values = await driver.read([PointRef(device_id="test-dev", point_id="nope")])

        assert values[0].quality == Quality.BAD

    async def test_read_sum_index_address_raises_not_implemented(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config())  # read_mode default "sum"
        driver.set_points_mapping([_make_index_point("speed")])
        driver._connection = FakeConnection()
        driver._connected = True

        with pytest.raises(NotImplementedError, match="symbol"):
            await driver.read([PointRef(device_id="test-dev", point_id="speed")])

    async def test_read_sum_overall_failure_raises_protocol_error(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping([_make_symbol_point("a", "MAIN.a")])
        driver._connection = FakeConnection()
        driver._connected = True

        def _boom(names: list[str]) -> dict[str, object]:
            raise RuntimeError("service not supported")

        monkeypatch.setattr(driver._connection, "read_list_by_name", _boom)

        with pytest.raises(ProtocolError, match="Sum read failed"):
            await driver.read([PointRef(device_id="test-dev", point_id="a")])


class TestReadSequential:
    async def test_read_sequential_uses_index_read(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config(read_mode="sequential"))
        driver.set_points_mapping([_make_index_point("speed")])
        driver._connection = FakeConnection()
        driver._connected = True

        # read() -> read(index_group, index_offset, plctype) → None from the fake.
        values = await driver.read([PointRef(device_id="test-dev", point_id="speed")])

        assert values[0].quality == Quality.GOOD
