"""Modbus 动态测点（未登记点表的 ModbusPoint）读写与缓存隔离测试。"""

from __future__ import annotations

import pytest

from core.application import ConfigError, Quality
from core.application.errors import ProtocolError
from core.application.protocol_contract import ProtocolWrite
from core.application.recovery import RecoveringProtocol, RecoverySettings
from core.infrastructure.protocol.modbus.mapping import modbus_point
from tests.unit.shared_core.test_modbus_single_ops import (
    _Client,
    _driver,
    _float32_registers,
    _point,
    _Response,
)

# ---------------------------------------------------------------------------
# modbus_point 工厂校验
# ---------------------------------------------------------------------------


class TestModbusPointFactory:
    def test_normalizes_register_type_alias_and_defaults(self) -> None:
        point = modbus_point("p", register_type="Holding_Register", address=10, data_type="uint16")
        assert point.register_type == "holding"
        assert point.count == 1
        assert point.word_order == "big_endian"

    def test_float32_defaults_to_two_registers(self) -> None:
        point = modbus_point("p", register_type="holding", address=0, data_type="float32")
        assert point.count == 2

    def test_rejects_count_mismatch(self) -> None:
        with pytest.raises(ConfigError, match="does not match"):
            modbus_point("p", register_type="holding", address=0, data_type="float32", count=1)

    def test_rejects_address_out_of_range(self) -> None:
        with pytest.raises(ConfigError, match="0..65535"):
            modbus_point("p", register_type="holding", address=0x10000, data_type="uint16")

    def test_rejects_span_overflow(self) -> None:
        with pytest.raises(ConfigError, match="span exceeds"):
            modbus_point("p", register_type="holding", address=0xFFFF, data_type="float32")

    def test_rejects_invalid_register_type(self) -> None:
        with pytest.raises(ConfigError, match="invalid Modbus register type"):
            modbus_point("p", register_type="coils", address=0, data_type="bool")

    def test_rejects_invalid_word_order(self) -> None:
        with pytest.raises(ConfigError, match="word_order"):
            modbus_point(
                "p",
                register_type="holding",
                address=0,
                data_type="float32",
                word_order="middle_endian",
            )

    def test_rejects_unknown_data_type(self) -> None:
        with pytest.raises(ConfigError, match="unsupported data_type"):
            modbus_point("p", register_type="holding", address=0, data_type="string")


# ---------------------------------------------------------------------------
# 动态点读取
# ---------------------------------------------------------------------------


class TestDynamicRead:
    @pytest.mark.asyncio
    async def test_read_one_with_dynamic_point(self) -> None:
        client = _Client()
        client.response = _Response(registers=[42])
        driver = _driver({}, client)
        dynamic = modbus_point("dyn", register_type="holding", address=200, data_type="uint16")

        sample = await driver.read_one(dynamic)

        assert client.read_holding_calls == [(200, 1)]
        assert sample.point_id == "dyn"
        assert sample.value == 42
        assert sample.quality == Quality.GOOD
        assert sample.timestamp.tzinfo is not None
        assert sample.timestamp_source == "local"

    @pytest.mark.asyncio
    async def test_read_many_mixed_registered_and_dynamic(self) -> None:
        client = _Client()
        client.response = _Response(registers=[1, 2])
        driver = _driver({"reg": _point("reg", "holding", 100)}, client)
        dynamic = modbus_point("dyn", register_type="holding", address=101, data_type="uint16")

        samples = await driver.read_many(["reg", dynamic])

        # 相邻地址合并为一次请求
        assert client.total_read_calls() == 1
        assert client.read_holding_calls == [(100, 2)]
        assert [s.point_id for s in samples] == ["reg", "dyn"]
        assert [s.value for s in samples] == [1, 2]

    @pytest.mark.asyncio
    async def test_read_many_preserves_order_and_duplicates(self) -> None:
        client = _Client()
        client.response = _Response(registers=[7])
        driver = _driver({}, client)
        dynamic = modbus_point("dyn", register_type="holding", address=100, data_type="uint16")

        samples = await driver.read_many([dynamic, dynamic, dynamic])

        assert client.total_read_calls() == 1
        assert len(samples) == 3
        assert all(s.value == 7 for s in samples)

    @pytest.mark.asyncio
    async def test_same_point_id_different_addresses_do_not_collide(self) -> None:
        """同 point_id 不同地址的动态点：各自返回各自地址的值，不互相覆盖。"""
        client = _Client()
        client.response = _Response(registers=[11, 22])
        driver = _driver({}, client)
        first = modbus_point("dup", register_type="holding", address=100, data_type="uint16")
        second = modbus_point("dup", register_type="holding", address=101, data_type="uint16")

        samples = await driver.read_many([first, second])

        assert client.read_holding_calls == [(100, 2)]
        assert [s.value for s in samples] == [11, 22]

    @pytest.mark.asyncio
    async def test_dynamic_point_does_not_collide_with_registered_point(self) -> None:
        """动态点与注册点同名不同地址：值按地址定义隔离。"""
        client = _Client()
        client.response = _Response(registers=[11, 22])
        driver = _driver({"p": _point("p", "holding", 100)}, client)
        dynamic = modbus_point("p", register_type="holding", address=200, data_type="uint16")

        # 地址不连续（gap 100 > 8），拆成两组请求，各自响应最后一条 registers
        responses = [_Response(registers=[11]), _Response(registers=[22])]

        original = client.read_holding_registers

        async def sequenced(address: int, *, count: int, device_id: int) -> _Response:
            await original.__self__._maybe_raise(None)  # type: ignore[attr-defined]
            client.read_holding_calls.append((address, count))
            return responses.pop(0)

        client.read_holding_registers = sequenced  # type: ignore[method-assign]

        samples = await driver.read_many(["p", dynamic])

        assert [s.value for s in samples] == [11, 22]
        assert samples[0].point_id == "p"
        assert samples[1].point_id == "p"

    @pytest.mark.asyncio
    async def test_plan_cache_isolates_dynamic_and_registered_keys(self) -> None:
        """同名动态点与注册点的读取计划缓存键不同，不互相命中。"""
        client = _Client()
        client.response = _Response(registers=[1])
        driver = _driver({"p": _point("p", "holding", 100)}, client)
        dynamic = modbus_point("p", register_type="holding", address=200, data_type="uint16")

        await driver.read_many(["p"])
        await driver.read_many([dynamic])

        assert ("p",) in driver._read_plan_cache
        assert (dynamic,) in driver._read_plan_cache
        assert len(driver._read_plan_cache) == 2
        # 两条计划地址不同
        assert driver._read_plan_cache[("p",)][0].start == 100
        assert driver._read_plan_cache[(dynamic,)][0].start == 200

    @pytest.mark.asyncio
    async def test_dynamic_decode_failure_returns_bad_sample(self) -> None:
        client = _Client()
        client.response = _Response(registers=[])  # 空响应段
        driver = _driver({}, client)
        dynamic = modbus_point("dyn", register_type="holding", address=100, data_type="uint16")

        sample = await driver.read_one(dynamic)

        assert sample.quality == Quality.BAD
        assert sample.value is None
        assert sample.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# 动态点写入
# ---------------------------------------------------------------------------


class TestDynamicWrite:
    @pytest.mark.asyncio
    async def test_write_one_with_modbus_point_and_value(self) -> None:
        client = _Client()
        driver = _driver({}, client)
        dynamic = modbus_point("dyn", register_type="holding", address=300, data_type="int16")

        result = await driver.write_one(dynamic, -5)

        assert result.success
        assert result.point_id == "dyn"
        assert client.write_register_calls == [(300, 0xFFFB)]

    @pytest.mark.asyncio
    async def test_write_one_with_registered_point_id_and_value(self) -> None:
        client = _Client()
        driver = _driver({"h": _point("h", "holding", 200, data_type="int16")}, client)

        result = await driver.write_one("h", 10)

        assert result.success
        assert client.write_register_calls == [(200, 10)]

    @pytest.mark.asyncio
    async def test_write_one_protocol_write_form_still_works(self) -> None:
        client = _Client()
        driver = _driver({"h": _point("h", "holding", 200, data_type="int16")}, client)

        result = await driver.write_one(ProtocolWrite("h", 3))

        assert result.success
        assert client.write_register_calls == [(200, 3)]

    @pytest.mark.asyncio
    async def test_write_one_protocol_write_with_extra_value_rejected(self) -> None:
        client = _Client()
        driver = _driver({"h": _point("h", "holding", 200)}, client)

        with pytest.raises(TypeError, match="does not take a separate value"):
            await driver.write_one(ProtocolWrite("h", 3), 4)  # type: ignore[call-overload]

        assert client.total_write_calls() == 0

    @pytest.mark.asyncio
    async def test_write_one_dynamic_missing_value_rejected(self) -> None:
        client = _Client()
        driver = _driver({}, client)
        dynamic = modbus_point("dyn", register_type="holding", address=300, data_type="int16")

        with pytest.raises(TypeError, match="requires an explicit value"):
            await driver.write_one(dynamic)  # type: ignore[call-overload]

        assert client.total_write_calls() == 0

    @pytest.mark.asyncio
    async def test_write_one_dynamic_multi_register_uses_fc16(self) -> None:
        client = _Client()
        driver = _driver({}, client)
        dynamic = modbus_point("dyn", register_type="holding", address=400, data_type="float32")

        result = await driver.write_one(dynamic, 42.5)

        assert result.success
        assert client.write_registers_calls == [(400, _float32_registers(42.5))]
        assert client.total_write_calls() == 1

    @pytest.mark.asyncio
    async def test_write_one_dynamic_read_only_rejected_before_send(self) -> None:
        client = _Client()
        driver = _driver({}, client)
        dynamic = modbus_point("dyn", register_type="input", address=100, data_type="int16")

        result = await driver.write_one(dynamic, 1)

        assert not result.success
        assert "read-only" in (result.message or "")
        assert client.total_write_calls() == 0

    @pytest.mark.asyncio
    async def test_write_one_dynamic_value_range_checked_before_send(self) -> None:
        client = _Client()
        driver = _driver({}, client)
        dynamic = modbus_point("dyn", register_type="holding", address=100, data_type="uint16")

        result = await driver.write_one(dynamic, 70000)

        assert not result.success
        assert client.total_write_calls() == 0

    @pytest.mark.asyncio
    async def test_write_one_does_not_delegate_to_write_many(self) -> None:
        client = _Client()
        driver = _driver({"h": _point("h", "holding", 100)}, client)

        async def fail_write_many(writes: object) -> tuple[()]:
            raise AssertionError("write_one must not delegate to write_many")

        driver.write_many = fail_write_many  # type: ignore[method-assign]
        result = await driver.write_one("h", 1)

        assert result.success
        assert client.total_write_calls() == 1


# ---------------------------------------------------------------------------
# 动态点经过 RecoveringProtocol 包装
# ---------------------------------------------------------------------------


class TestDynamicPointsThroughRecovery:
    @pytest.mark.asyncio
    async def test_recovering_protocol_reconnects_before_dynamic_read(self) -> None:
        """断线状态下经 RecoveringProtocol 读动态点：先恢复连接再读。"""
        client = _Client()
        client.response = _Response(registers=[9])
        driver = _driver({}, client, connected=False)

        reconnects = 0

        async def connect() -> None:
            nonlocal reconnects
            reconnects += 1
            # 恢复路径先 close（释放旧 client），connect 需重建 client 视图
            client.connected = True
            driver._client = client
            driver._connected = True

        driver.connect = connect  # type: ignore[method-assign]
        wrapped = RecoveringProtocol(driver, RecoverySettings(read_retries=1))
        dynamic = modbus_point("dyn", register_type="holding", address=100, data_type="uint16")

        sample = await wrapped.read_one(dynamic)  # type: ignore[arg-type]

        assert reconnects == 1
        assert sample.value == 9

    @pytest.mark.asyncio
    async def test_recovering_protocol_dynamic_write_not_resent_after_failure(self) -> None:
        """写发送后断线：RecoveringProtocol 不重发，错误原样抛出。"""
        client = _Client()
        client.write_error = ConnectionError("reset by peer")
        driver = _driver({}, client)
        wrapped = RecoveringProtocol(driver, RecoverySettings(read_retries=1))
        dynamic = modbus_point("dyn", register_type="holding", address=100, data_type="int16")

        with pytest.raises(ProtocolError, match="write failed"):
            await wrapped.write_one(dynamic, 1)  # type: ignore[call-overload]

        assert client.total_write_calls() == 1
