"""D5 回归：FC15 批量线圈写入、跨 16-bit 边界、多点安全拒绝与 FC01 回读。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.gateway_harness import (
    COLLECTOR_IP,
    COMMAND_COIL,
    COMMAND_COIL_B,
    COMMAND_FLOAT,
    COMMANDER_IP,
    FakeCommanderService,
    build_config,
    reserve_port,
    start_gateway,
)

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


@pytest.fixture
async def harness(tmp_path: Path):
    commander = FakeCommanderService()
    config = build_config(reserve_port(), reserve_port(), tmp_path / "journal.json")
    gateway = await start_gateway(config, commander)
    try:
        yield gateway
    finally:
        await gateway.stop()


async def test_fc15_single_feedback_coil(harness) -> None:
    collector = await harness.client(COLLECTOR_IP)
    try:
        result = await collector.write_coil(0, True, device_id=1)
        assert not result.isError(), result
        read = await collector.read_coils(0, count=1, device_id=1)
        assert not read.isError() and read.bits[0] is True
    finally:
        collector.close()


async def test_fc15_multiple_adjacent_feedback_coils(harness) -> None:
    collector = await harness.client(COLLECTOR_IP)
    try:
        result = await collector.write_coils(0, [True, False], device_id=1)
        assert not result.isError(), result
        read = await collector.read_coils(0, count=2, device_id=1)
        assert not read.isError()
        assert read.bits[:2] == [True, False]
    finally:
        collector.close()


async def test_fc15_crosses_16bit_boundary(harness) -> None:
    """线圈 15/16/17 跨越 16-bit 字边界，一次批量写入并回读。"""
    collector = await harness.client(COLLECTOR_IP)
    try:
        result = await collector.write_coils(15, [True, False, True], device_id=1)
        assert not result.isError(), result
        read = await collector.read_coils(15, count=3, device_id=1)
        assert not read.isError(), read
        assert read.bits[:3] == [True, False, True]
    finally:
        collector.close()


async def test_fc15_incomplete_and_unmapped_region_rejected(harness) -> None:
    collector = await harness.client(COLLECTOR_IP)
    try:
        # 仅线圈 0/1 映射，覆盖到未映射线圈 2 必须拒绝。
        gap = await collector.write_coils(0, [True, True, True], device_id=1)
        assert gap.isError()
        assert gap.exception_code == 0x02  # ILLEGAL DATA ADDRESS
        # 完全未映射的区域同样拒绝。
        hole = await collector.write_coil(5, True, device_id=1)
        assert hole.isError()
        assert hole.exception_code == 0x02

        # 被拒绝后，已映射线圈不得被部分改写。
        seeded = await collector.write_coils(0, [True, True], device_id=1)
        assert not seeded.isError()
        rejected = await collector.write_coils(0, [False, False, False], device_id=1)
        assert rejected.isError()
        read = await collector.read_coils(0, count=2, device_id=1)
        assert read.bits[:2] == [True, True]
    finally:
        collector.close()


async def test_same_coil_address_independent_across_units(harness) -> None:
    c1 = await harness.client(COLLECTOR_IP)
    try:
        await c1.write_coils(15, [True, True, True], device_id=1)
        await c1.write_coils(15, [False, False, False], device_id=2)
        r1 = await c1.read_coils(15, count=3, device_id=1)
        r2 = await c1.read_coils(15, count=3, device_id=2)
        assert r1.bits[:3] == [True, True, True]
        assert r2.bits[:3] == [False, False, False]
    finally:
        c1.close()


async def test_fc15_multiple_command_points_safely_rejected(harness) -> None:
    """一次 FC15 覆盖两个控制点：必须整体拒绝，零 Commander 调用、零镜像提交。"""
    commander: FakeCommanderService = harness.commander
    dc_ems = await harness.client(COMMANDER_IP)
    try:
        result = await dc_ems.write_coils(COMMAND_COIL, [True, True], device_id=1)
        assert result.isError()
        assert commander.calls == []
        # 两个命令线圈镜像均不得出现真值。
        for address in (COMMAND_COIL, COMMAND_COIL_B):
            read = await dc_ems.read_coils(address, count=1, device_id=1)
            assert read.isError()  # 空镜像 → DEVICE_FAILURE，绝不能是写入值
    finally:
        dc_ems.close()


async def test_fc16_multiple_command_points_safely_rejected(harness) -> None:
    """一次 FC16 跨 cmd_float(200-201)+cmd_uint2(202) 两个控制点：整体拒绝。"""
    commander: FakeCommanderService = harness.commander
    dc_ems = await harness.client(COMMANDER_IP)
    try:
        result = await dc_ems.write_registers(COMMAND_FLOAT, [0x41A0, 0x0000, 5], device_id=1)
        assert result.isError()
        assert commander.calls == []
    finally:
        dc_ems.close()


async def test_fc01_readback_matches_point_store(harness) -> None:
    """FC01 整段回读的位投影与 FC15/FC05 写入的 Point Store 一致。"""
    collector = await harness.client(COLLECTOR_IP)
    try:
        assert not (await collector.write_coils(0, [False, True], device_id=1)).isError()
        assert not (await collector.write_coils(15, [True, False, True], device_id=1)).isError()
        read = await collector.read_coils(0, count=18, device_id=1)
        assert not read.isError(), read
        bits = read.bits[:18]
        assert bits[0] is False
        assert bits[1] is True
        assert bits[15:18] == [True, False, True]
        # 未写过的映射线圈读异常（无陈旧数据）。
        fresh = await collector.read_coils(0, count=2, device_id=9)
        assert fresh.isError()
    finally:
        collector.close()


async def test_fc15_command_writer_cannot_touch_feedback(harness) -> None:
    dc_ems = await harness.client(COMMANDER_IP)
    try:
        result = await dc_ems.write_coils(0, [True], device_id=1)
        assert result.isError()
    finally:
        dc_ems.close()
    assert harness.commander.calls == []
