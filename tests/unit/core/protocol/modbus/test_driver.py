"""Unit tests for Modbus driver — mocked pymodbus client."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

import wind_hub_core.protocol.modbus.driver as modbus_driver_module
from wind_hub_core.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub_core.model.command import Command
from wind_hub_core.model.errors import ProtocolError
from wind_hub_core.model.point import PointRef, Quality
from wind_hub_core.protocol.modbus.driver import ModbusDriver


def _make_device_config(**extensions: object) -> DeviceConfig:
    return DeviceConfig(
        device_id="test-dev",
        protocol="modbus",
        point_table="t1",
        endpoint=Endpoint(
            host="127.0.0.1",
            port=502,
            extensions=dict(extensions),
        ),
    )


def _make_point_config(
    point_id: str,
    register_type: str,
    address: int,
    data_type: str = "float32",
) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(register_type=register_type, address=address),
        data_type=data_type,
    )


def _float32_registers(value: float) -> list[int]:
    """Encode a float32 into two big-endian register words."""
    return list(struct.unpack(">HH", struct.pack(">f", value)))


class _FakeResponse:
    def __init__(
        self,
        bits: list[bool] | None = None,
        registers: list[int] | None = None,
        error: bool = False,
    ) -> None:
        self._bits = bits or []
        self._registers = registers or []
        self._error = error

    @property
    def bits(self) -> list[bool]:
        return self._bits

    @property
    def registers(self) -> list[int]:
        return self._registers

    def isError(self) -> bool:  # noqa: N802 — 对应 pymodbus 的 `response.isError()`
        return self._error


class _FakeClient:
    def __init__(self) -> None:
        self.connect = AsyncMock(return_value=True)
        self.close = MagicMock()
        self.read_coils = AsyncMock()
        self.read_discrete_inputs = AsyncMock()
        self.read_holding_registers = AsyncMock()
        self.read_input_registers = AsyncMock()
        self.write_coil = AsyncMock()
        self.write_register = AsyncMock()
        self.write_registers = AsyncMock()


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr("pymodbus.client.AsyncModbusTcpClient", lambda *a, **k: client)


# ---------------------------------------------------------------------------
# connect / close / health
# ---------------------------------------------------------------------------


class TestConnect:
    async def test_connect_and_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        _patch_client(monkeypatch, client)
        driver = ModbusDriver(_make_device_config())

        await driver.connect()
        assert client.connect.await_count == 1
        assert driver.health().healthy is True

        await driver.close()
        assert client.close.called
        assert driver.health().healthy is False

    async def test_connect_failure_is_single_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient()
        client.connect = AsyncMock(return_value=False)
        _patch_client(monkeypatch, client)

        driver = ModbusDriver(_make_device_config())
        with pytest.raises(ProtocolError, match="failed to connect"):
            await driver.connect()

        assert client.connect.await_count == 1
        assert driver.health().healthy is False

    async def test_rtu_mode_raises_not_implemented(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        _patch_client(monkeypatch, client)
        driver = ModbusDriver(_make_device_config(mode="rtu"))
        with pytest.raises(NotImplementedError, match="RTU"):
            await driver.connect()


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestRead:
    async def test_read_holding_float32(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        client.read_holding_registers.return_value = _FakeResponse(
            registers=_float32_registers(42.5)
        )
        _patch_client(monkeypatch, client)

        driver = ModbusDriver(_make_device_config(word_order="big_endian"))
        driver.set_points_mapping([_make_point_config("gen.power", "holding", 100)])

        await driver.connect()
        values = await driver.read([PointRef(device_id="test-dev", point_id="gen.power")])
        await driver.close()

        client.read_holding_registers.assert_awaited_once_with(100, count=2, device_id=1)
        assert values[0].value == pytest.approx(42.5)
        assert values[0].quality == Quality.GOOD
        assert values[0].source == "modbus"

    async def test_read_coil_bool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        client.read_coils.return_value = _FakeResponse(bits=[True])
        _patch_client(monkeypatch, client)

        driver = ModbusDriver(_make_device_config())
        driver.set_points_mapping([_make_point_config("sw.on", "coil", 5, data_type="bool")])

        await driver.connect()
        values = await driver.read([PointRef(device_id="test-dev", point_id="sw.on")])
        await driver.close()

        assert values[0].value is True
        assert values[0].quality == Quality.GOOD

    async def test_read_unknown_point_bad_quality(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        _patch_client(monkeypatch, client)
        driver = ModbusDriver(_make_device_config())

        await driver.connect()
        values = await driver.read([PointRef(device_id="test-dev", point_id="no.point")])
        await driver.close()

        assert values[0].value is None
        assert values[0].quality == Quality.BAD

    async def test_read_not_connected_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, _FakeClient())
        driver = ModbusDriver(_make_device_config())
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.read([PointRef(device_id="test-dev", point_id="p")])

    async def test_read_merges_consecutive_addresses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        # Two adjacent float32 points (100, 102) → one 4-register read.
        client.read_holding_registers.return_value = _FakeResponse(
            registers=_float32_registers(1.5) + _float32_registers(2.5)
        )
        _patch_client(monkeypatch, client)

        driver = ModbusDriver(_make_device_config(word_order="big_endian"))
        driver.set_points_mapping(
            [
                _make_point_config("a", "holding", 100),
                _make_point_config("b", "holding", 102),
            ]
        )

        await driver.connect()
        values = await driver.read(
            [
                PointRef(device_id="test-dev", point_id="a"),
                PointRef(device_id="test-dev", point_id="b"),
            ]
        )
        await driver.close()

        client.read_holding_registers.assert_awaited_once_with(100, count=4, device_id=1)
        assert values[0].value == pytest.approx(1.5)
        assert values[1].value == pytest.approx(2.5)

    async def test_short_response_marks_only_undecodable_point_bad(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """批量读部分失败：整组请求成功但响应偏短——可解码的点 GOOD，
        解不出的点单独 BAD（value=None），批次顺序与数量不变。"""
        client = _FakeClient()
        # 两个相邻 float32 点（100, 102）合并成一次 4 寄存器读，
        # 但响应只回了 2 个寄存器——第二个点解不出。
        client.read_holding_registers.return_value = _FakeResponse(
            registers=_float32_registers(1.5)
        )
        _patch_client(monkeypatch, client)

        driver = ModbusDriver(_make_device_config(word_order="big_endian"))
        driver.set_points_mapping(
            [
                _make_point_config("a", "holding", 100),
                _make_point_config("b", "holding", 102),
            ]
        )

        await driver.connect()
        values = await driver.read(
            [
                PointRef(device_id="test-dev", point_id="a"),
                PointRef(device_id="test-dev", point_id="b"),
            ]
        )
        await driver.close()

        assert len(values) == 2  # 批次不缩
        assert values[0].quality == Quality.GOOD
        assert values[0].value == pytest.approx(1.5)
        assert values[1].quality == Quality.BAD
        assert values[1].value is None


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    async def test_write_holding_float32(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        client.write_registers.return_value = _FakeResponse()
        _patch_client(monkeypatch, client)

        driver = ModbusDriver(_make_device_config(word_order="big_endian"))
        driver.set_points_mapping([_make_point_config("setpoint", "holding", 300)])

        await driver.connect()
        results = await driver.write(
            [
                Command(
                    command_id="c1",
                    device_id="test-dev",
                    point_id="setpoint",
                    value=42.5,
                )
            ]
        )
        await driver.close()

        assert len(results) == 1
        assert results[0].command_id == "c1"
        assert results[0].success is True
        assert results[0].error is None
        client.write_registers.assert_awaited_once_with(300, _float32_registers(42.5), device_id=1)

    async def test_write_coil_bool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        client.write_coil.return_value = _FakeResponse()
        _patch_client(monkeypatch, client)

        driver = ModbusDriver(_make_device_config())
        driver.set_points_mapping([_make_point_config("sw", "coil", 10, data_type="bool")])

        await driver.connect()
        results = await driver.write(
            [Command(command_id="c1", device_id="test-dev", point_id="sw", value=True)]
        )
        await driver.close()

        assert results[0].success is True
        client.write_coil.assert_awaited_once_with(10, True, device_id=1)

    async def test_write_unknown_point_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, _FakeClient())
        driver = ModbusDriver(_make_device_config())

        await driver.connect()
        results = await driver.write(
            [Command(command_id="c1", device_id="test-dev", point_id="nope", value=True)]
        )
        await driver.close()

        assert results[0].success is False
        assert "unknown point" in (results[0].error or "")

    async def test_write_read_only_point_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, _FakeClient())
        driver = ModbusDriver(_make_device_config())
        driver.set_points_mapping([_make_point_config("di", "discrete_input", 1, data_type="bool")])

        await driver.connect()
        results = await driver.write(
            [Command(command_id="c1", device_id="test-dev", point_id="di", value=True)]
        )
        await driver.close()

        assert results[0].success is False

    async def test_write_not_connected_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, _FakeClient())
        driver = ModbusDriver(_make_device_config())
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.write(
                [Command(command_id="c1", device_id="test-dev", point_id="p", value=1.0)]
            )

    async def test_write_empty_returns_empty(self) -> None:
        driver = ModbusDriver(_make_device_config())
        assert await driver.write([]) == []


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------


class TestSubscribe:
    async def test_subscribe_raises_not_implemented(self) -> None:
        driver = ModbusDriver(_make_device_config())

        async def cb(value: object) -> None:  # type: ignore[arg-type]
            pass

        with pytest.raises(NotImplementedError):
            await driver.subscribe([], cb)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 重连循环日志（决策 2）：超时标记为 timed out 的简洁 warning
# ---------------------------------------------------------------------------


import logging  # noqa: E402


class TestReconnectLogging:
    async def test_connect_timeout_logs_concise_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        driver = ModbusDriver(_make_device_config())

        async def _timeout_connect() -> None:
            raise TimeoutError("timed out")

        monkeypatch.setattr(driver, "_do_connect", _timeout_connect)
        with caplog.at_level(logging.WARNING, logger=modbus_driver_module.__name__):
            with pytest.raises(ProtocolError, match="failed to connect"):
                await driver.connect()

        timeout_logs = [r for r in caplog.records if "timed out" in r.message]
        assert len(timeout_logs) == 1
        assert timeout_logs[0].exc_info is None


# ---------------------------------------------------------------------------
# word_order
# ---------------------------------------------------------------------------


def _int32_registers_le(value: int) -> list[int]:
    """int32 → 两个寄存器，little_endian：低地址寄存器 = 低 16 位。"""
    return [value & 0xFFFF, (value >> 16) & 0xFFFF]


class TestWordOrder:
    async def test_device_default_little_endian_int32_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """设备默认 word_order=little_endian：低地址寄存器为低 16 位。"""
        client = _FakeClient()
        client.read_input_registers.return_value = _FakeResponse(
            registers=_int32_registers_le(123456)
        )
        _patch_client(monkeypatch, client)

        driver = ModbusDriver(_make_device_config())  # 默认 little_endian
        driver.set_points_mapping([_make_point_config("p", "input", 178, data_type="int32")])

        await driver.connect()
        values = await driver.read([PointRef(device_id="test-dev", point_id="p")])
        await driver.close()

        assert values[0].value == 123456
        assert values[0].quality == Quality.GOOD

    async def test_point_word_order_overrides_device_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """点级 word_order=big_endian 覆盖设备默认 little_endian。"""
        client = _FakeClient()
        client.read_input_registers.return_value = _FakeResponse(
            registers=list(reversed(_int32_registers_le(123456)))  # 高字在前
        )
        _patch_client(monkeypatch, client)

        point = _make_point_config("p", "input", 178, data_type="int32").model_copy(
            update={
                "address": PointAddress(register_type="input", address=178, word_order="big_endian")
            }
        )
        driver = ModbusDriver(_make_device_config())  # 设备默认 little_endian
        driver.set_points_mapping([point])

        await driver.connect()
        values = await driver.read([PointRef(device_id="test-dev", point_id="p")])
        await driver.close()

        assert values[0].value == 123456

    async def test_write_encode_matches_read_decode_little_endian(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """little_endian 下写入 encode 与读取 decode 语义一致（回环）。"""
        client = _FakeClient()
        client.write_registers.return_value = _FakeResponse()
        _patch_client(monkeypatch, client)

        driver = ModbusDriver(_make_device_config())  # 默认 little_endian
        driver.set_points_mapping([_make_point_config("sp", "holding", 300, data_type="int32")])

        await driver.connect()
        results = await driver.write(
            [Command(command_id="c1", device_id="test-dev", point_id="sp", value=123456)]
        )
        assert results[0].success is True
        written = client.write_registers.await_args.args[1]
        assert written == _int32_registers_le(123456)

        # 同一组寄存器读回应解出同一值
        client.read_holding_registers.return_value = _FakeResponse(registers=written)
        values = await driver.read([PointRef(device_id="test-dev", point_id="sp")])
        await driver.close()
        assert values[0].value == 123456

    async def test_write_encode_matches_read_decode_big_endian(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """big_endian 设备：写入/读取回环一致，低地址寄存器 = 高 16 位。"""
        client = _FakeClient()
        client.write_registers.return_value = _FakeResponse()
        _patch_client(monkeypatch, client)

        driver = ModbusDriver(_make_device_config(word_order="big_endian"))
        driver.set_points_mapping([_make_point_config("sp", "holding", 300, data_type="int32")])

        await driver.connect()
        await driver.write(
            [Command(command_id="c1", device_id="test-dev", point_id="sp", value=123456)]
        )
        written = client.write_registers.await_args.args[1]
        assert written == list(reversed(_int32_registers_le(123456)))  # 高字在前

        client.read_holding_registers.return_value = _FakeResponse(registers=written)
        values = await driver.read([PointRef(device_id="test-dev", point_id="sp")])
        await driver.close()
        assert values[0].value == 123456
