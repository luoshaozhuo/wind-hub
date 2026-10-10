"""Modbus 单点读写独立实现与批量读取回归测试（mock pymodbus client）。"""

from __future__ import annotations

import asyncio
import struct
import subprocess
import sys

import pytest

from core.application import ConfigError
from core.application.errors import ProtocolError
from core.application.protocol_contract import ProtocolWrite
from core.application.recovery import RecoveringProtocol, RecoverySettings
from core.domain import (
    UNIT_CATALOG,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    UnitCode,
)
from core.infrastructure.protocol.modbus.config import parse_modbus_config
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from core.infrastructure.protocol.modbus.mapping import ModbusPoint

try:
    from pymodbus.exceptions import ModbusIOException
except ImportError:  # pymodbus 是可选依赖
    ModbusIOException = None

_requires_pymodbus = pytest.mark.skipif(
    ModbusIOException is None, reason="pymodbus not installed"
)


def _point(
    point_id: str,
    register_type: str = "holding",
    address: int = 100,
    *,
    count: int = 1,
    data_type: str = "uint16",
    word_order: str = "big_endian",
) -> ModbusPoint:
    return ModbusPoint(
        point_id=point_id,
        register_type=register_type,
        address=address,
        count=count,
        data_type=data_type,
        word_order=word_order,
    )


class _Response:
    def __init__(
        self,
        *,
        bits: list[bool] | None = None,
        registers: list[int] | None = None,
        error: bool = False,
    ) -> None:
        self.bits = bits or []
        self.registers = registers or []
        self._error = error

    def isError(self) -> bool:  # noqa: N802 - pymodbus response API
        return self._error


class _Client:
    def __init__(self) -> None:
        self.read_coils_calls: list[tuple[int, int]] = []
        self.read_discrete_calls: list[tuple[int, int]] = []
        self.read_holding_calls: list[tuple[int, int]] = []
        self.read_input_calls: list[tuple[int, int]] = []
        self.write_coil_calls: list[tuple[int, bool]] = []
        self.write_register_calls: list[tuple[int, int]] = []
        self.write_registers_calls: list[tuple[int, list[int]]] = []
        self.close_calls = 0
        self.response = _Response()
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.write_response_error = False
        self.connected = True

    def close(self) -> None:
        self.close_calls += 1
        self.connected = False

    async def _maybe_raise(self, error: Exception | None) -> None:
        if error is not None:
            raise error

    async def read_coils(self, address: int, *, count: int, device_id: int) -> _Response:
        assert device_id == 1
        self.read_coils_calls.append((address, count))
        await self._maybe_raise(self.read_error)
        return self.response

    async def read_discrete_inputs(
        self, address: int, *, count: int, device_id: int
    ) -> _Response:
        assert device_id == 1
        self.read_discrete_calls.append((address, count))
        await self._maybe_raise(self.read_error)
        return self.response

    async def read_holding_registers(
        self, address: int, *, count: int, device_id: int
    ) -> _Response:
        assert device_id == 1
        self.read_holding_calls.append((address, count))
        await self._maybe_raise(self.read_error)
        return self.response

    async def read_input_registers(
        self, address: int, *, count: int, device_id: int
    ) -> _Response:
        assert device_id == 1
        self.read_input_calls.append((address, count))
        await self._maybe_raise(self.read_error)
        return self.response

    async def write_coil(self, address: int, value: bool, *, device_id: int) -> _Response:
        assert device_id == 1
        self.write_coil_calls.append((address, value))
        await self._maybe_raise(self.write_error)
        return _Response(error=self.write_response_error)

    async def write_register(self, address: int, value: int, *, device_id: int) -> _Response:
        assert device_id == 1
        self.write_register_calls.append((address, value))
        await self._maybe_raise(self.write_error)
        return _Response(error=self.write_response_error)

    async def write_registers(
        self, address: int, values: list[int], *, device_id: int
    ) -> _Response:
        assert device_id == 1
        self.write_registers_calls.append((address, values))
        await self._maybe_raise(self.write_error)
        return _Response(error=self.write_response_error)

    def total_read_calls(self) -> int:
        return (
            len(self.read_coils_calls)
            + len(self.read_discrete_calls)
            + len(self.read_holding_calls)
            + len(self.read_input_calls)
        )

    def total_write_calls(self) -> int:
        return (
            len(self.write_coil_calls)
            + len(self.write_register_calls)
            + len(self.write_registers_calls)
        )


def _driver(
    points: dict[str, ModbusPoint],
    client: _Client,
    *,
    connected: bool = True,
) -> ModbusDriver:
    driver = object.__new__(ModbusDriver)
    driver._config = parse_modbus_config(ConnectionEndpoint("192.0.2.10", 502), {})
    driver._point_table_id = "test"
    driver._points = points
    driver._read_plan_cache = {}
    driver._client = client
    driver._connected = connected
    driver._lock = asyncio.Lock()
    return driver


def _float32_registers(value: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", value)))


# ---------------------------------------------------------------------------
# read_one
# ---------------------------------------------------------------------------


class TestReadOne:
    @pytest.mark.asyncio
    async def test_read_coil(self) -> None:
        client = _Client()
        client.response = _Response(bits=[True])
        driver = _driver({"b": _point("b", "coil", 5, data_type="bool")}, client)

        sample = await driver.read_one("b")

        assert client.read_coils_calls == [(5, 1)]
        assert client.total_read_calls() == 1
        assert sample.value is True
        assert sample.quality == "good"

    @pytest.mark.asyncio
    async def test_read_discrete_input(self) -> None:
        client = _Client()
        client.response = _Response(bits=[False])
        driver = _driver(
            {"d": _point("d", "discrete_input", 7, data_type="bool")}, client
        )

        sample = await driver.read_one("d")

        assert client.read_discrete_calls == [(7, 1)]
        assert sample.value is False

    @pytest.mark.asyncio
    async def test_read_holding_single_register(self) -> None:
        client = _Client()
        client.response = _Response(registers=[4321])
        driver = _driver({"h": _point("h", "holding", 100)}, client)

        sample = await driver.read_one("h")

        assert client.read_holding_calls == [(100, 1)]
        assert sample.value == 4321

    @pytest.mark.asyncio
    async def test_read_input_multi_register_float32(self) -> None:
        """float32 占 2 个寄存器，仍只属于一次 read_one 请求。"""
        client = _Client()
        client.response = _Response(registers=_float32_registers(42.5))
        driver = _driver(
            {"f": _point("f", "input", 200, count=2, data_type="float32")}, client
        )

        sample = await driver.read_one("f")

        assert client.read_input_calls == [(200, 2)]
        assert client.total_read_calls() == 1
        assert sample.value == pytest.approx(42.5)

    @pytest.mark.asyncio
    async def test_read_one_does_not_use_plan_cache(self) -> None:
        client = _Client()
        client.response = _Response(registers=[1])
        driver = _driver({"h": _point("h")}, client)

        await driver.read_one("h")

        assert driver._read_plan_cache == {}

    @pytest.mark.asyncio
    async def test_decode_failure_returns_bad_quality(self) -> None:
        client = _Client()
        client.response = _Response(registers=[])  # 空段 → 解码失败
        driver = _driver({"h": _point("h")}, client)

        sample = await driver.read_one("h")

        assert sample.value is None
        assert sample.quality == "bad"
        assert driver.health().healthy is True  # 解码失败 ≠ 断线

    @pytest.mark.asyncio
    async def test_exception_response_raises_without_disconnect(self) -> None:
        client = _Client()
        client.response = _Response(error=True)
        driver = _driver({"h": _point("h")}, client)

        with pytest.raises(ProtocolError, match="exception response"):
            await driver.read_one("h")

        assert driver.health().healthy is True

    @pytest.mark.asyncio
    async def test_communication_error_marks_disconnect(self) -> None:
        client = _Client()
        client.read_error = ConnectionError("reset by peer")
        driver = _driver({"h": _point("h")}, client)

        with pytest.raises(ProtocolError, match="read failed"):
            await driver.read_one("h")

        assert driver.health().healthy is False
        assert client.close_calls == 1

    @pytest.mark.asyncio
    async def test_read_one_requires_connection(self) -> None:
        driver = _driver({"h": _point("h")}, _Client(), connected=False)
        with pytest.raises(ProtocolError, match="active connection"):
            await driver.read_one("h")

    @pytest.mark.asyncio
    async def test_read_one_unknown_point(self) -> None:
        driver = _driver({}, _Client())
        with pytest.raises(ConfigError, match="not part of connection"):
            await driver.read_one("ghost")


# ---------------------------------------------------------------------------
# read_many 回归：分组、缓存、顺序、部分解码失败
# ---------------------------------------------------------------------------


class TestReadMany:
    @pytest.mark.asyncio
    async def test_consecutive_addresses_merge_and_order_preserved(self) -> None:
        client = _Client()
        client.response = _Response(registers=_float32_registers(1.5) + _float32_registers(2.5))
        points = {
            "a": _point("a", "holding", 100, count=2, data_type="float32"),
            "b": _point("b", "holding", 102, count=2, data_type="float32"),
        }
        driver = _driver(points, client)

        samples = await driver.read_many(("b", "a"))

        assert client.total_read_calls() == 1
        assert client.read_holding_calls == [(100, 4)]
        assert [s.point_id for s in samples] == ["b", "a"]
        assert samples[0].value == pytest.approx(2.5)
        assert samples[1].value == pytest.approx(1.5)

    @pytest.mark.asyncio
    async def test_different_register_types_split_groups(self) -> None:
        client = _Client()

        async def holding(address: int, *, count: int, device_id: int) -> _Response:
            return _Response(registers=[1])

        async def coils(address: int, *, count: int, device_id: int) -> _Response:
            return _Response(bits=[True])

        client.read_holding_registers = holding  # type: ignore[method-assign]
        client.read_coils = coils  # type: ignore[method-assign]
        points = {
            "h": _point("h", "holding", 100),
            "c": _point("c", "coil", 100, data_type="bool"),
        }
        driver = _driver(points, client)

        samples = await driver.read_many(("h", "c"))

        assert [s.point_id for s in samples] == ["h", "c"]
        assert all(s.quality == "good" for s in samples)

    @pytest.mark.asyncio
    async def test_read_plan_cache_hit_skips_regrouping(self) -> None:
        client = _Client()
        client.response = _Response(registers=[1])
        driver = _driver({"h": _point("h")}, client)

        await driver.read_many(("h",))
        assert ("h",) in driver._read_plan_cache
        await driver.read_many(("h",))
        assert client.total_read_calls() == 2  # 缓存只复用规划，请求仍发送

    @pytest.mark.asyncio
    async def test_point_table_update_invalidates_cache(self) -> None:
        client = _Client()
        client.response = _Response(registers=[1])
        driver = _driver({"h": _point("h")}, client)
        await driver.read_many(("h",))
        assert driver._read_plan_cache

        point = Point(
            point_id="h",
            business_point_id="h",
            source_unit=UNIT_CATALOG[UnitCode.NONE],
            access=PointAccess.READ,
            ext={"register_type": "holding", "address": 100, "data_type": "uint16"},
        )
        driver.update_point_table(PointTable("pt", Protocol("modbus"), {"h": point}))
        assert driver._read_plan_cache == {}

    @pytest.mark.asyncio
    async def test_partial_decode_failure_marks_single_point_bad(self) -> None:
        client = _Client()
        # 两点合并读取但响应只够解第一个点。
        client.response = _Response(registers=_float32_registers(1.5))
        points = {
            "a": _point("a", "holding", 100, count=2, data_type="float32"),
            "b": _point("b", "holding", 102, count=2, data_type="float32"),
        }
        driver = _driver(points, client)

        samples = await driver.read_many(("a", "b"))

        assert samples[0].quality == "good"
        assert samples[1].quality == "bad"
        assert samples[1].value is None

    @pytest.mark.asyncio
    async def test_read_many_communication_error_marks_disconnect(self) -> None:
        client = _Client()
        client.read_error = ConnectionError("reset by peer")
        driver = _driver({"h": _point("h")}, client)

        with pytest.raises(ProtocolError, match="read failed"):
            await driver.read_many(("h",))

        assert driver.health().healthy is False


# ---------------------------------------------------------------------------
# write_one
# ---------------------------------------------------------------------------


class TestWriteOne:
    @pytest.mark.asyncio
    async def test_write_coil(self) -> None:
        client = _Client()
        driver = _driver({"c": _point("c", "coil", 10, data_type="bool")}, client)

        result = await driver.write_one(ProtocolWrite("c", True))

        assert result.success
        assert client.write_coil_calls == [(10, True)]
        assert client.total_write_calls() == 1

    @pytest.mark.asyncio
    async def test_write_single_holding_register(self) -> None:
        client = _Client()
        driver = _driver({"h": _point("h", "holding", 200, data_type="int16")}, client)

        result = await driver.write_one(ProtocolWrite("h", -5))

        assert result.success
        assert client.write_register_calls == [(200, 0xFFFB)]

    @pytest.mark.asyncio
    async def test_write_multi_register_point_uses_single_fc16(self) -> None:
        """float32 逻辑点用一次 write_registers(FC16) 写入。"""
        client = _Client()
        driver = _driver(
            {"f": _point("f", "holding", 300, count=2, data_type="float32")}, client
        )

        result = await driver.write_one(ProtocolWrite("f", 42.5))

        assert result.success
        assert client.write_registers_calls == [(300, _float32_registers(42.5))]
        assert client.write_register_calls == []
        assert client.total_write_calls() == 1

    @pytest.mark.asyncio
    async def test_read_only_register_rejected_without_send(self) -> None:
        client = _Client()
        driver = _driver({"i": _point("i", "input", 100, data_type="int16")}, client)

        result = await driver.write_one(ProtocolWrite("i", 1))

        assert not result.success
        assert "read-only" in (result.message or "")
        assert client.total_write_calls() == 0

    @pytest.mark.asyncio
    async def test_invalid_value_rejected_without_send(self) -> None:
        client = _Client()
        driver = _driver({"h": _point("h", "holding", 100)}, client)

        result = await driver.write_one(ProtocolWrite("h", 70000))

        assert not result.success
        assert client.total_write_calls() == 0
        assert driver.health().healthy is True  # 校验失败 ≠ 断线

    @pytest.mark.asyncio
    async def test_exception_response_fails_write_but_keeps_connection(self) -> None:
        client = _Client()
        client.write_response_error = True
        driver = _driver({"h": _point("h", "holding", 100)}, client)

        result = await driver.write_one(ProtocolWrite("h", 10))

        assert not result.success
        assert result.message == "Modbus exception response"
        assert driver.health().healthy is True

    @pytest.mark.asyncio
    async def test_communication_error_marks_disconnect_single_send(self) -> None:
        """写请求发送后断线：只发一次，Driver 不自动重发。"""
        client = _Client()
        client.write_error = ConnectionError("reset by peer")
        driver = _driver({"h": _point("h", "holding", 100)}, client)

        with pytest.raises(ProtocolError, match="write failed"):
            await driver.write_one(ProtocolWrite("h", 10))

        assert client.total_write_calls() == 1
        assert driver.health().healthy is False

    @pytest.mark.asyncio
    async def test_write_one_requires_connection(self) -> None:
        client = _Client()
        driver = _driver({"h": _point("h")}, client, connected=False)
        with pytest.raises(ProtocolError, match="active connection"):
            await driver.write_one(ProtocolWrite("h", 1))
        assert client.total_write_calls() == 0

    @pytest.mark.asyncio
    async def test_write_one_unknown_point(self) -> None:
        client = _Client()
        driver = _driver({}, client)
        with pytest.raises(ConfigError, match="not part of connection"):
            await driver.write_one(ProtocolWrite("ghost", 1))
        assert client.total_write_calls() == 0


# ---------------------------------------------------------------------------
# 取消/超时后的连接状态一致性
# ---------------------------------------------------------------------------


class TestCancellationState:
    @pytest.mark.asyncio
    async def test_cancelled_read_marks_disconnect_and_propagates(self) -> None:
        client = _Client()
        client.read_error = asyncio.CancelledError()
        driver = _driver({"h": _point("h")}, client)

        with pytest.raises(asyncio.CancelledError):
            await driver.read_one("h")

        assert driver.health().healthy is False
        assert client.close_calls == 1

    @pytest.mark.asyncio
    async def test_cancelled_write_marks_disconnect_and_propagates(self) -> None:
        client = _Client()
        client.write_error = asyncio.CancelledError()
        driver = _driver({"h": _point("h")}, client)

        with pytest.raises(asyncio.CancelledError):
            await driver.write_one(ProtocolWrite("h", 1))

        assert client.total_write_calls() == 1  # 已发送的请求绝不重发
        assert driver.health().healthy is False

    @_requires_pymodbus
    @pytest.mark.asyncio
    async def test_pymodbus_wrapped_cancellation_is_unwrapped(self) -> None:
        """pymodbus 3.15 把 CancelledError 转成 ModbusIOException；Driver 必须
        还原取消语义（超时→TimeoutError / 主动取消→传播），同时失效连接。"""
        client = _Client()
        wrapped = ModbusIOException("Request cancelled outside library.")
        wrapped.__cause__ = asyncio.CancelledError()
        client.read_error = wrapped
        driver = _driver({"h": _point("h")}, client)

        with pytest.raises(asyncio.CancelledError):
            await driver.read_one("h")

        assert driver.health().healthy is False

    @pytest.mark.asyncio
    async def test_read_timeout_marks_disconnect_via_recovery(self) -> None:  # noqa: D103
        """经 RecoveringProtocol 的 read 超时：连接必须失效，不允许残留健康状态。

        重连目标指向关闭端口，确保恢复路径确定性失败。
        """
        client = _Client()

        async def hanging_read(address: int, *, count: int, device_id: int) -> _Response:
            await asyncio.sleep(5)
            return _Response()

        client.read_holding_registers = hanging_read  # type: ignore[method-assign]
        driver = _driver({"h": _point("h")}, client)
        driver._config = parse_modbus_config(ConnectionEndpoint("127.0.0.1", 1), {})
        port = RecoveringProtocol(driver, RecoverySettings(read_timeout=0.01))

        with pytest.raises(ProtocolError):
            await port.read_one("h")

        assert driver.health().healthy is False


# ---------------------------------------------------------------------------
# 点表热更新与读写的映射一致性
# ---------------------------------------------------------------------------


def _domain_point(point_id: str, address: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ_WRITE,
        ext={"register_type": "holding", "address": address, "data_type": "uint16"},
    )


class TestPointTableHotUpdate:
    """update_point_table 是同步整体置换：单 event loop 内不存在交错，
    更新后的下一次读写必须使用新映射。"""

    @staticmethod
    def _updatable_driver(points: list[Point], client: _Client) -> ModbusDriver:
        driver = ModbusDriver(
            ConnectionEndpoint("192.0.2.10", 502),
            PointTable("pt", Protocol("modbus"), {p.point_id: p for p in points}),
            {},
        )
        driver._client = client
        driver._connected = True
        return driver

    @pytest.mark.asyncio
    async def test_read_one_uses_new_address_after_update(self) -> None:
        client = _Client()
        client.response = _Response(registers=[7])
        driver = self._updatable_driver([_domain_point("h", 100)], client)

        driver.update_point_table(
            PointTable("pt", Protocol("modbus"), {"h": _domain_point("h", 200)})
        )
        await driver.read_one("h")

        assert client.read_holding_calls == [(200, 1)]

    @pytest.mark.asyncio
    async def test_write_one_uses_new_address_after_update(self) -> None:
        client = _Client()
        driver = self._updatable_driver([_domain_point("h", 100)], client)

        driver.update_point_table(
            PointTable("pt", Protocol("modbus"), {"h": _domain_point("h", 200)})
        )
        await driver.write_one(ProtocolWrite("h", 10))

        assert client.write_register_calls == [(200, 10)]

    @pytest.mark.asyncio
    async def test_removed_point_is_rejected_immediately(self) -> None:
        client = _Client()
        driver = self._updatable_driver([_domain_point("h", 100)], client)

        driver.update_point_table(PointTable("pt", Protocol("modbus"), {}))
        with pytest.raises(ConfigError, match="not part of connection"):
            await driver.read_one("h")
        with pytest.raises(ConfigError, match="not part of connection"):
            await driver.write_one(ProtocolWrite("h", 1))
        assert client.total_write_calls() == 0


# ---------------------------------------------------------------------------
# write_many 明确不支持
# ---------------------------------------------------------------------------


class TestWriteManyUnsupported:
    @pytest.mark.asyncio
    async def test_empty_input_raises(self) -> None:
        client = _Client()
        driver = _driver({}, client)
        with pytest.raises(NotImplementedError, match="write_many"):
            await driver.write_many(())
        assert client.total_write_calls() == 0

    @pytest.mark.asyncio
    async def test_non_empty_input_raises(self) -> None:
        client = _Client()
        driver = _driver({"h": _point("h")}, client)
        with pytest.raises(NotImplementedError, match="write_many"):
            await driver.write_many((ProtocolWrite("h", 1),))
        assert client.total_write_calls() == 0


# ---------------------------------------------------------------------------
# write_groups 配置已随批量写移除
# ---------------------------------------------------------------------------


def test_write_groups_option_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown Modbus options"):
        parse_modbus_config(
            ConnectionEndpoint("192.0.2.10", 502),
            {"write_groups": [["a", "b"]]},
        )


def test_protocol_modules_import_without_pymodbus() -> None:
    """pymodbus 缺失时 Modbus/ADS/IEC104 模块仍可导入（可选依赖隔离）。"""
    script = (
        "import sys;\n"
        "sys.modules['pymodbus'] = None;\n"
        "sys.modules['pymodbus.client'] = None;\n"
        "from core.infrastructure.protocol.modbus.driver import ModbusDriver;\n"
        "from core.infrastructure.protocol.ads.driver import ADSDriver;\n"
        "from core.infrastructure.protocol.iec104.driver import IEC104Driver;\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
