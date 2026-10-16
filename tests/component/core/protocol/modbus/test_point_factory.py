"""ModbusPoint 工厂校验与统一 Point Key 契约测试。

统一契约后不存在“动态点”读写：read_one/read_many/subscribe 只接受
PointTable 已配置的 Point Key（str），write_one 通过 ProtocolWrite 携带
str Key。配置点的读写行为在 test_single_ops.py 完整覆盖；本文件保留
modbus_point 工厂自身的入参校验，并验证旧的 ModbusPoint 直传入口已关闭。
"""

from __future__ import annotations

import pytest

from core.application import ConfigError
from core.application.protocol_contract import ProtocolWrite
from core.infrastructure.protocol.modbus.mapping import modbus_point

from .test_single_ops import _Client, _driver, _point, _Response

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
# 旧动态点入口（直接传 ModbusPoint）已关闭
# ---------------------------------------------------------------------------


class TestObjectPointAPIRejected:
    @pytest.mark.asyncio
    async def test_read_one_rejects_point_object(self) -> None:
        client = _Client()
        driver = _driver({}, client)
        dynamic = modbus_point("dyn", register_type="holding", address=200, data_type="uint16")

        with pytest.raises(ConfigError, match="configured point key"):
            await driver.read_one(dynamic)  # type: ignore[arg-type]  # 故意传入旧对象 API

        assert client.total_read_calls() == 0

    @pytest.mark.asyncio
    async def test_read_many_rejects_point_object(self) -> None:
        client = _Client()
        driver = _driver({"reg": _point("reg", "holding", 100)}, client)
        dynamic = modbus_point("dyn", register_type="holding", address=101, data_type="uint16")

        with pytest.raises(ConfigError, match="configured point key"):
            await driver.read_many(["reg", dynamic])  # type: ignore[list-item]  # 旧对象 API

        assert client.total_read_calls() == 0

    def test_protocol_write_rejects_point_object(self) -> None:
        dynamic = modbus_point("dyn", register_type="holding", address=300, data_type="int16")

        with pytest.raises(ValueError, match="configured point key"):
            ProtocolWrite(dynamic, -5)  # type: ignore[arg-type]  # 旧对象 API

    @pytest.mark.asyncio
    async def test_read_plan_cache_is_bounded_for_configured_keys(self) -> None:
        """读取计划缓存仍按 key 序列限界，防止长期轮询积累无界内存。"""
        client = _Client()
        client.response = _Response(registers=[1])
        points = {f"p{address}": _point(f"p{address}", "holding", address) for address in range(64)}
        driver = _driver(points, client)

        for address in range(64):
            await driver.read_many((f"p{address}",))

        assert len(driver._read_plan_cache) == 32
        assert client.total_read_calls() == 64
