"""ADS 地址型 Sum Read 与 sequential 分派单元测试。"""

from __future__ import annotations

import ctypes
import struct
from types import SimpleNamespace

import pyads
import pytest

from wind_hub.adapter.outbound.protocol.ads.driver import ADSDriver
from wind_hub.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub.domain.model.errors import ProtocolError
from wind_hub.domain.model.point import PointRef, Quality


def _make_device_config(read_mode: str = "sum", **extensions: object) -> DeviceConfig:
    return DeviceConfig(
        device_id="test-dev",
        protocol="ads",
        point_table="t1",
        endpoint=Endpoint(
            host="192.168.0.100",
            port=48898,
            extensions=dict(extensions),
        ),
        read_mode=read_mode,
    )


def _make_symbol_point(point_id: str, symbol: str) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(symbol=symbol),
        data_type="float32",
    )


def _make_index_point(point_id: str, offset: int = 0) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(index_group=0x4020, index_offset=offset),
        data_type="float32",
    )


class FakeConnection:
    """最小 pyads.Connection 替身，只提供 symbol 解析与 sequential read。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.is_open = False
        self.values: dict[tuple[int, int], object] = {}
        self.symbols = {
            "MAIN.rotorSpeed": (0x4020, 100),
            "MAIN.genPower": (0x4020, 104),
            "MAIN.a": (0x4020, 108),
            "MAIN.b": (0x4020, 112),
            "MAIN.c": (0x4020, 116),
            "MAIN.ok": (0x4020, 120),
        }

    def set_timeout(self, ms: int) -> None:
        del ms

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def get_symbol(self, symbol: str) -> object:
        if symbol == "MAIN.broken":
            raise pyads.ADSError(1808, "symbol not found")
        index_group, index_offset = self.symbols[symbol]
        return SimpleNamespace(
            index_group=index_group,
            index_offset=index_offset,
            plc_type=ctypes.c_float,
            symbol_type="REAL",
        )

    def read(self, index_group: int, index_offset: int, plc_datatype: object) -> object:
        del plc_datatype
        return self.values.get((index_group, index_offset))


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyads.Connection", FakeConnection)


def _sum_payload(values: list[float], errors: list[int] | None = None) -> bytes:
    errors = errors or [0] * len(values)
    return b"".join(struct.pack("<I", error) for error in errors) + b"".join(
        struct.pack("<f", value) for value in values
    )


class TestReadSum:
    async def test_read_sum_resolves_symbols_then_reads_addresses(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping(
            [
                _make_symbol_point("rotor.speed", "MAIN.rotorSpeed"),
                _make_symbol_point("gen.power", "MAIN.genPower"),
            ]
        )
        await driver.connect()
        seen: list[list[tuple[int, int, int]]] = []

        def _sum(addresses: list[tuple[int, int, int]]) -> bytes:
            seen.append(addresses)
            return _sum_payload([1200.5, 800.0])

        monkeypatch.setattr(driver, "_sum_read_bytes", _sum)
        values = await driver.read(
            [
                PointRef(device_id="test-dev", point_id="rotor.speed"),
                PointRef(device_id="test-dev", point_id="gen.power"),
            ]
        )
        await driver.close()

        assert [v.value for v in values] == pytest.approx([1200.5, 800.0])
        assert seen == [[(0x4020, 100, 4), (0x4020, 104, 4)]]
        assert all(v.quality == Quality.GOOD for v in values)

    async def test_read_sum_batches_by_max_subs_per_sum(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config(max_subs_per_sum=2))
        driver.set_points_mapping(
            [
                _make_symbol_point("a", "MAIN.a"),
                _make_symbol_point("b", "MAIN.b"),
                _make_symbol_point("c", "MAIN.c"),
            ]
        )
        await driver.connect()
        seen: list[list[tuple[int, int, int]]] = []

        def _sum(addresses: list[tuple[int, int, int]]) -> bytes:
            seen.append(addresses)
            return _sum_payload([float(i + 1) for i in range(len(addresses))])

        monkeypatch.setattr(driver, "_sum_read_bytes", _sum)
        await driver.read(
            [
                PointRef(device_id="test-dev", point_id="a"),
                PointRef(device_id="test-dev", point_id="b"),
                PointRef(device_id="test-dev", point_id="c"),
            ]
        )
        await driver.close()

        assert [len(batch) for batch in seen] == [2, 1]

    async def test_read_sum_unresolved_symbol_is_bad(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping(
            [
                _make_symbol_point("ok", "MAIN.ok"),
                _make_symbol_point("broken", "MAIN.broken"),
            ]
        )
        await driver.connect()
        monkeypatch.setattr(driver, "_sum_read_bytes", lambda addresses: _sum_payload([7.5]))

        values = await driver.read(
            [
                PointRef(device_id="test-dev", point_id="ok"),
                PointRef(device_id="test-dev", point_id="broken"),
            ]
        )
        await driver.close()

        assert values[0].value == pytest.approx(7.5)
        assert values[0].quality == Quality.GOOD
        assert values[1].value is None
        assert values[1].quality == Quality.BAD

    async def test_read_sum_index_only_point_uses_configured_address(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping([_make_index_point("speed", 16)])
        await driver.connect()
        seen: list[list[tuple[int, int, int]]] = []

        def _sum(addresses: list[tuple[int, int, int]]) -> bytes:
            seen.append(addresses)
            return _sum_payload([12.0])

        monkeypatch.setattr(driver, "_sum_read_bytes", _sum)
        values = await driver.read([PointRef(device_id="test-dev", point_id="speed")])
        await driver.close()

        assert values[0].value == pytest.approx(12.0)
        assert seen == [[(0x4020, 16, 4)]]

    async def test_read_sum_subcommand_error_marks_only_point_bad(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping([_make_index_point("a", 0), _make_index_point("b", 4)])
        await driver.connect()
        monkeypatch.setattr(
            driver,
            "_sum_read_bytes",
            lambda addresses: _sum_payload([1.0, 2.0], errors=[0, 1808]),
        )

        values = await driver.read(
            [
                PointRef(device_id="test-dev", point_id="a"),
                PointRef(device_id="test-dev", point_id="b"),
            ]
        )
        await driver.close()

        assert values[0].quality == Quality.GOOD
        assert values[1].quality == Quality.BAD

    async def test_read_sum_overall_failure_raises_protocol_error(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping([_make_index_point("a")])
        await driver.connect()

        def _boom(addresses: list[tuple[int, int, int]]) -> bytes:
            del addresses
            raise RuntimeError("service not supported")

        monkeypatch.setattr(driver, "_sum_read_bytes", _boom)

        with pytest.raises(ProtocolError, match="ADS read failed"):
            await driver.read([PointRef(device_id="test-dev", point_id="a")])
        await driver.close()


class TestReadSequential:
    async def test_read_sequential_uses_index_read(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config(read_mode="sequential"))
        driver.set_points_mapping([_make_index_point("speed")])
        await driver.connect()
        driver._connection.values[(0x4020, 0)] = 3.0

        values = await driver.read([PointRef(device_id="test-dev", point_id="speed")])
        await driver.close()

        assert values[0].value == pytest.approx(3.0)
        assert values[0].quality == Quality.GOOD
