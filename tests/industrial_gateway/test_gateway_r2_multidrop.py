"""D2 回归：单 Endpoint 12 Unit ID、同 TCP 连接多 Unit、未配置 Unit 隔离。

真实 pymodbus 3.15.0 TCP 客户端 + 真实 Gateway 服务端 + 真实 grpc.aio Commander，
不安装任何垫片（复现旧版 setMultidrop AttributeError / TCP RST 必须直接暴露）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.gateway_harness import (
    COLLECTOR_IP,
    COMMANDER_IP,
    FEEDBACK_FLOAT,
    FakeCommanderService,
    build_config,
    float_words,
    reserve_port,
    start_gateway,
)

pytestmark = [pytest.mark.integration, pytest.mark.modbus, pytest.mark.real_service]


@pytest.fixture
async def harness(tmp_path: Path):
    commander = FakeCommanderService()
    config = build_config(reserve_port(), reserve_port(), tmp_path / "journal.json")
    gateway = await start_gateway(config, commander)
    try:
        yield gateway
    finally:
        await gateway.stop()


async def test_twelve_units_all_respond_on_single_connection(harness) -> None:
    """同一条 TCP 连接上顺序访问全部 12 个 Unit ID，不得 RST/异常。"""
    client = await harness.client(COLLECTOR_IP)
    try:
        for unit in range(1, 13):
            value = 100.0 + unit
            words = float_words(value)
            write = await client.write_registers(FEEDBACK_FLOAT, words, device_id=unit)
            assert not write.isError(), f"unit {unit} write failed: {write}"
        # 在同一条连接上回读，每个 Unit 的数据必须互相隔离。
        for unit in range(1, 13):
            read = await client.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=unit)
            assert not read.isError(), f"unit {unit} read failed: {read}"
            assert read.registers == float_words(
                100.0 + unit
            ), f"unit {unit} data leaked: {read.registers}"
    finally:
        client.close()


async def test_interleaved_units_same_connection(harness) -> None:
    """同一连接交错访问不同 Unit，响应必须逐帧对应，不得串扰。"""
    client = await harness.client(COLLECTOR_IP)
    try:
        for unit in (1, 12, 1, 12, 7, 3, 7, 3):
            write = await client.write_registers(
                FEEDBACK_FLOAT, float_words(float(unit)), device_id=unit
            )
            assert not write.isError()
        for unit, expected in ((1, 1.0), (3, 3.0), (7, 7.0), (12, 12.0)):
            read = await client.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=unit)
            assert not read.isError()
            assert read.registers == float_words(expected)
    finally:
        client.close()


async def test_unconfigured_unit_returns_exception_without_affecting_others(harness) -> None:
    """未配置的 Unit ID 返回合理 Modbus 异常，同连接后续合法 Unit 仍正常。"""
    client = await harness.client(COLLECTOR_IP)
    try:
        ok = await client.write_registers(FEEDBACK_FLOAT, float_words(42.5), device_id=5)
        assert not ok.isError()

        bad = await client.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=99)
        assert bad.isError()
        # 0x0B = GATEWAY TARGET DEVICE FAILED TO RESPOND（目标从站无响应）
        assert bad.exception_code == 0x0B

        good = await client.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=5)
        assert not good.isError()
        assert good.registers == float_words(42.5)
    finally:
        client.close()


async def test_two_independent_connections(harness) -> None:
    """两条 TCP 连接（不同来源身份）并发访问，连接之间互不影响。"""
    collector = await harness.client(COLLECTOR_IP)
    dc_ems = await harness.client(COMMANDER_IP)
    try:
        w1 = await collector.write_registers(FEEDBACK_FLOAT, float_words(9.0), device_id=1)
        assert not w1.isError()
        # DC-EMS 身份无 data_writer 权限，写反馈必须拒绝。
        w2 = await dc_ems.write_registers(FEEDBACK_FLOAT, float_words(9.0), device_id=1)
        assert w2.isError()
        read = await collector.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=1)
        assert read.registers == float_words(9.0)
    finally:
        collector.close()
        dc_ems.close()
