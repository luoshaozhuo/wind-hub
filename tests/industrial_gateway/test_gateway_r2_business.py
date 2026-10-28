"""完整业务链路、过期失效、来源权限、UNKNOWN 重启闭锁与默认控制禁用。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from contracts import commander_pb2 as pb
from tests.support.gateway_harness import (
    COLLECTOR_IP,
    COMMAND_UINT,
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


async def test_full_feedback_and_command_chain(harness) -> None:
    """Collector 写反馈 → DC-EMS 读反馈；DC-EMS 写命令 → Commander(模拟 PLC)。"""
    commander: FakeCommanderService = harness.commander
    collector = await harness.client(COLLECTOR_IP)
    dc_ems = await harness.client(COMMANDER_IP)
    try:
        value = 1234.5
        written = await collector.write_registers(FEEDBACK_FLOAT, float_words(value), device_id=1)
        assert not written.isError()

        read = await dc_ems.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=1)
        assert not read.isError()
        assert read.registers == float_words(value)

        cmd = await dc_ems.write_register(COMMAND_UINT, 808, device_id=1)
        assert not cmd.isError(), cmd
        assert len(commander.calls) == 1
        call = commander.calls[0]
        assert (call.device_id, call.point_id, call.value, call.timeout) == (
            "phys1",
            "spu",
            808,
            3.0,
        )
    finally:
        collector.close()
        dc_ems.close()


async def test_feedback_expires_after_max_age(tmp_path: Path) -> None:
    commander = FakeCommanderService()
    config = build_config(reserve_port(), reserve_port(), tmp_path / "j.json", max_age_seconds=0.3)
    gateway = await start_gateway(config, commander)
    try:
        collector = await gateway.client(COLLECTOR_IP)
        dc_ems = await gateway.client(COMMANDER_IP)
        try:
            assert not (
                await collector.write_registers(FEEDBACK_FLOAT, float_words(1.0), device_id=1)
            ).isError()
            fresh = await dc_ems.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=1)
            assert not fresh.isError() and fresh.registers == float_words(1.0)
            await asyncio.sleep(0.45)
            stale = await dc_ems.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=1)
            # 过期后不允许继续返回旧值。
            assert stale.isError() and stale.exception_code == 0x04
        finally:
            collector.close()
            dc_ems.close()
    finally:
        await gateway.stop()


async def test_unauthorized_peer_rejected(harness) -> None:
    """不在 ACL 中的来源（127.0.0.1）任何写都必须拒绝，读不受限。"""
    stranger = await harness.client("127.0.0.1")
    collector = await harness.client(COLLECTOR_IP)
    try:
        denied = await stranger.write_registers(FEEDBACK_FLOAT, float_words(1.0), device_id=1)
        assert denied.isError() and denied.exception_code == 0x02
        denied_cmd = await stranger.write_register(COMMAND_UINT, 1, device_id=1)
        assert denied_cmd.isError()

        assert not (
            await collector.write_registers(FEEDBACK_FLOAT, float_words(2.0), device_id=1)
        ).isError()
        read = await stranger.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=1)
        assert not read.isError() and read.registers == float_words(2.0)
    finally:
        stranger.close()
        collector.close()


async def test_unknown_lock_survives_restart(tmp_path: Path) -> None:
    """UNKNOWN 后重启：日志残留命令必须继续闭锁，重启后重发零 Commander 调用。"""
    journal = tmp_path / "journal.json"
    commander = FakeCommanderService()
    commander.set_outcome("phys1", "spu", pb.COMMAND_OUTCOME_UNKNOWN)
    config = build_config(reserve_port(), reserve_port(), journal)
    gateway = await start_gateway(config, commander)
    try:
        dc_ems = await gateway.client(COMMANDER_IP)
        result = await dc_ems.write_register(COMMAND_UINT, 42, device_id=1)
        assert result.isError()
        dc_ems.close()
        assert journal.exists() and journal.read_text()
    finally:
        await gateway.stop()

    # 模拟进程重启：全新 Commander（观测新调用）+ 同一持久日志。
    restarted_commander = FakeCommanderService()
    restarted = await start_gateway(config, restarted_commander)
    try:
        dc_ems = await restarted.client(COMMANDER_IP)
        retry = await dc_ems.write_register(COMMAND_UINT, 42, device_id=1)
        assert retry.isError() and retry.exception_code == 0x04
        # 闭锁点不得向现场重发；同设备另一控制点恢复正常。
        assert restarted_commander.calls == []
        other = await dc_ems.write_register(COMMAND_UINT, 7, device_id=2)
        assert not other.isError(), other
        assert len(restarted_commander.calls) == 1
    finally:
        dc_ems.close()
        await restarted.stop()


async def test_disabled_control_has_no_command_entry(tmp_path: Path) -> None:
    """control.enabled=false：command_writers 强制为空，不存在可绕过的控制入口。"""
    commander = FakeCommanderService()
    config = build_config(
        reserve_port(), reserve_port(), tmp_path / "j.json", control_enabled=False
    )
    gateway = await start_gateway(config, commander)
    try:
        assert gateway.application.commander_channel is None
        dc_ems = await gateway.client(COMMANDER_IP)
        collector = await gateway.client(COLLECTOR_IP)
        try:
            # 即便来源 IP 写在"命令写入方"语义位置，也没有任何命令写入身份。
            denied = await dc_ems.write_register(COMMAND_UINT, 1, device_id=1)
            assert denied.isError()
            denied_coil = await dc_ems.write_coil(32, True, device_id=1)
            assert denied_coil.isError()
            # 反馈链路仍然正常工作。
            ok = await collector.write_registers(FEEDBACK_FLOAT, float_words(3.0), device_id=1)
            assert not ok.isError()
        finally:
            dc_ems.close()
            collector.close()
        assert commander.calls == []
    finally:
        await gateway.stop()


async def test_rpc_error_treated_as_unknown_and_persisted(tmp_path: Path) -> None:
    """Commander 不可达（RPC 错误）必须按 UNKNOWN 处理：异常 + 闭锁 + 日志残留。"""
    commander = FakeCommanderService()
    config = build_config(
        reserve_port(),
        reserve_port(),
        tmp_path / "j.json",
        command_timeout=0.3,
        rpc_timeout=0.6,
    )
    gateway = await start_gateway(config, commander)
    journal = Path(config.control.journal_path)
    try:
        await gateway.grpc_server.stop(grace=0)  # Commander 直接下线
        dc_ems = await gateway.client(COMMANDER_IP)
        try:
            result = await dc_ems.write_register(COMMAND_UINT, 42, device_id=1)
            assert result.isError() and result.exception_code == 0x04
        finally:
            dc_ems.close()
        assert journal.exists() and journal.read_text().strip() not in ("", "{}")

        # 进程重启：先停旧应用（同一监听端口），再用同一配置+日志启动。
        await gateway.application.stop()
        restarted_commander = FakeCommanderService()
        restarted = await start_gateway(config, restarted_commander)
        try:
            dc_ems = await restarted.client(COMMANDER_IP)
            retry = await dc_ems.write_register(COMMAND_UINT, 42, device_id=1)
            assert retry.isError() and retry.exception_code == 0x04
            assert restarted_commander.calls == []
            dc_ems.close()
        finally:
            await restarted.stop()
    finally:
        await gateway.stop()


async def test_concurrent_commands_same_point_exactly_once_each(harness) -> None:
    """两条连接并发写同一控制点：逐点串行，每次写入恰好一次 RPC，无虚假失败。"""
    commander: FakeCommanderService = harness.commander
    commander.set_delay("phys1", "spu", 0.3)

    async def one(seq: int) -> None:
        dc_ems = await harness.client(COMMANDER_IP)
        try:
            result = await dc_ems.write_register(COMMAND_UINT, 100 + seq, device_id=1)
            assert not result.isError(), result
        finally:
            dc_ems.close()

    await asyncio.gather(*(one(i) for i in range(4)))
    calls = [c for c in commander.calls if c.point_id == "spu"]
    assert len(calls) == 4
    assert len({c.command_id for c in calls}) == 4
    assert commander.max_inflight("phys1", "spu") == 1


async def test_failure_isolation_between_points_and_devices(harness) -> None:
    """一个控制点确定失败 / UNKNOWN，不影响其他控制点与其他设备。"""
    commander: FakeCommanderService = harness.commander
    commander.set_outcome("phys1", "spu", pb.COMMAND_OUTCOME_CONFIRMED_FAILURE)
    dc_ems = await harness.client(COMMANDER_IP)
    try:
        bad = await dc_ems.write_register(COMMAND_UINT, 1, device_id=1)
        assert bad.isError()
        # 同设备另一控制点正常。
        coil = await dc_ems.write_coil(32, True, device_id=1)
        assert not coil.isError(), coil
        # 另一设备同点正常。
        other = await dc_ems.write_register(COMMAND_UINT, 1, device_id=2)
        assert not other.isError(), other
        assert len(commander.calls) == 3
    finally:
        dc_ems.close()
