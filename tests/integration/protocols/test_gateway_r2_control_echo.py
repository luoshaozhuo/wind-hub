"""D3 回归：FC05/FC06 回声一致性、Commander 恰好一次、三态与 UNKNOWN 闭锁。

铁律：现场已执行成功时 Gateway 必须返回成功；失败/UNKNOWN 绝不能返回虚假成功；
回声读取不得再次触发控制；UNKNOWN 闭锁后不得重发。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.infrastructure.rpc import commander_pb2 as pb
from tests.support.gateway_harness import (
    COMMAND_COIL,
    COMMAND_UINT,
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


async def test_fc06_success_echo_and_exactly_one_commander_call(harness) -> None:
    """FC06 成功：客户端收到正常响应，Commander 恰好 1 次，回声读不再触发。"""
    commander: FakeCommanderService = harness.commander
    client = await harness.client(COMMANDER_IP)
    try:
        result = await client.write_register(COMMAND_UINT, 1234, device_id=1)
        assert not result.isError(), result
        assert len(commander.calls) == 1
        call = commander.calls[0]
        assert (call.device_id, call.point_id) == ("phys1", "spu")
        assert call.kind == "uint64_value"
        assert call.value == 1234
        assert call.command_id

        # 回声读取：FC03 读命令点镜像，不允许再次调用 Commander。
        echo = await client.read_holding_registers(COMMAND_UINT, count=1, device_id=1)
        assert not echo.isError(), echo
        assert echo.registers == [1234]
        assert len(commander.calls) == 1
    finally:
        client.close()


async def test_fc05_success_echo_and_exactly_one_commander_call(harness) -> None:
    """FC05 成功：正常响应，Commander 恰好 1 次，FC01 回声值为 ON。"""
    commander: FakeCommanderService = harness.commander
    client = await harness.client(COMMANDER_IP)
    try:
        result = await client.write_coil(COMMAND_COIL, True, device_id=1)
        assert not result.isError(), result
        assert len(commander.calls) == 1
        call = commander.calls[0]
        assert (call.device_id, call.point_id) == ("phys1", "spb")
        assert call.kind == "bool_value"
        assert call.value is True

        echo = await client.read_coils(COMMAND_COIL, count=1, device_id=1)
        assert not echo.isError(), echo
        assert echo.bits[0] is True
        assert len(commander.calls) == 1
    finally:
        client.close()


async def test_confirmed_failure_no_false_success_and_no_mirror(harness) -> None:
    """Commander 确认失败：客户端必须收到异常，且不得写入成功值。"""
    commander: FakeCommanderService = harness.commander
    commander.set_outcome("phys1", "spu", pb.COMMAND_OUTCOME_CONFIRMED_FAILURE)
    client = await harness.client(COMMANDER_IP)
    try:
        result = await client.write_register(COMMAND_UINT, 77, device_id=1)
        assert result.isError()
        assert result.exception_code == 0x04  # SLAVE DEVICE FAILURE
        assert len(commander.calls) == 1

        # 确定失败后镜像中不得出现该值（读到 DEVICE_FAILURE 而非 77）。
        echo = await client.read_holding_registers(COMMAND_UINT, count=1, device_id=1)
        assert echo.isError()
        assert echo.exception_code == 0x04
    finally:
        client.close()


async def test_unknown_outcome_locks_point_and_blocks_resend(harness) -> None:
    """UNKNOWN：客户端异常 + 闭锁；后续重发不得再调用 Commander。"""
    commander: FakeCommanderService = harness.commander
    commander.set_outcome("phys1", "spu", pb.COMMAND_OUTCOME_UNKNOWN)
    client = await harness.client(COMMANDER_IP)
    try:
        result = await client.write_register(COMMAND_UINT, 77, device_id=1)
        assert result.isError()
        assert result.exception_code == 0x04
        assert len(commander.calls) == 1

        for _ in range(3):
            retry = await client.write_register(COMMAND_UINT, 77, device_id=1)
            assert retry.isError()
        # 闭锁后一次都不许重发。
        assert len(commander.calls) == 1

        # 同设备其他控制点、其他设备同点必须不受闭锁影响。
        ok_coil = await client.write_coil(COMMAND_COIL, False, device_id=1)
        assert not ok_coil.isError(), ok_coil
        ok_other = await client.write_register(COMMAND_UINT, 5, device_id=2)
        assert not ok_other.isError(), ok_other
    finally:
        client.close()


async def test_repeated_same_value_gets_distinct_command_ids(harness) -> None:
    """相同数值重复写入是真实的新命令：command_id 必须两两不同。"""
    commander: FakeCommanderService = harness.commander
    client = await harness.client(COMMANDER_IP)
    try:
        for _ in range(4):
            result = await client.write_register(COMMAND_UINT, 1234, device_id=1)
            assert not result.isError(), result
        ids = [call.command_id for call in commander.calls]
        assert len(ids) == 4
        assert len(set(ids)) == 4
    finally:
        client.close()


async def test_command_write_from_data_writer_rejected(harness) -> None:
    """Collector 身份不得写控制点，且不得触发 Commander。"""
    commander: FakeCommanderService = harness.commander
    client = await harness.client("127.0.0.2")
    try:
        result = await client.write_register(COMMAND_UINT, 1, device_id=1)
        assert result.isError()
        assert commander.calls == []
    finally:
        client.close()
