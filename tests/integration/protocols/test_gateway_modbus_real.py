"""industrial_gateway 真实 Modbus TCP 链路定向验收（应全部通过的行为）。

真实 Gateway Server（pymodbus 3.15 simulator）+ 真实 pymodbus 客户端 +
真实 grpc.aio Commander。两处生产缺陷由 tests.support.gateway_harness 的
测试期垫片绕过，详见该模块文档；缺陷本身的复现见
``test_gateway_modbus_defects.py``。

覆盖：
- FC01/03/15/16 Collector 写反馈 / DC-EMS 读反馈 / DC-EMS 写命令；
- 单 Endpoint 12 个 Unit ID 地址隔离；
- 来源权限矩阵、半点/空洞拒绝、未配置 Unit 拒绝；
- Commander 三态、重复命令逐点串行、故障隔离、UNKNOWN 重启闭锁；
- 并发下 Unit ID / 事务 ID / 客户端身份串扰。

注：FC05/FC06 当前因生产缺陷不可用，见缺陷复现文件。
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

import pytest
from pymodbus.constants import ExcCodes

from core.infrastructure.rpc import commander_pb2 as pb
from industrial_gateway.infrastructure.config.loader import load_config
from tests.support.gateway_harness import (
    COIL_CMD,
    COIL_FB_0,
    COLLECTOR_IP,
    COMMAND_FLOAT,
    COMMAND_UINT,
    COMMANDER_IP,
    FEEDBACK_FLOAT,
    FEEDBACK_PAIR,
    FEEDBACK_UINT,
    UNAUTHORIZED_IP,
    FakeCommanderService,
    build_config,
    float_words,
    install_server_shims,
    reserve_port,
    start_gateway,
    words_float,
)

pytestmark = [
    pytest.mark.real_service,
    pytest.mark.modbus,
    pytest.mark.fast,
]

UNIT_COUNT = 12
ALL_UNITS = list(range(1, UNIT_COUNT + 1))


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install_server_shims(monkeypatch)
    commander = FakeCommanderService()
    config = build_config(
        reserve_port(),
        reserve_port(),
        tmp_path / "commands.json",
        max_age_seconds=0.6,
    )
    runtime = await start_gateway(config, commander)
    try:
        yield runtime
    finally:
        await runtime.stop()


def _exc(response: object) -> int | None:
    if not response.isError():
        return None
    code = response.exception_code
    return int(code.value) if hasattr(code, "value") else int(code)


# --- FC01/03/15/16：Collector 写反馈，DC-EMS 读反馈 ------------------------


async def test_fc01_03_15_16_collector_write_and_dcems_read(harness) -> None:
    collector = await harness.client(COLLECTOR_IP)
    try:
        # FC16：完整 float32
        assert (
            _exc(await collector.write_registers(FEEDBACK_FLOAT, float_words(12.5), device_id=1))
            is None
        )
        # FC16：一次覆盖相邻 float32 + uint16 两个反馈点
        assert (
            _exc(
                await collector.write_registers(
                    FEEDBACK_FLOAT, float_words(8.0) + [77], device_id=1
                )
            )
            is None
        )
        # 单字写半个 float32 必须拒绝（FC06 链路另见缺陷文件）
        result = await collector.write_register(FEEDBACK_FLOAT, float_words(-40.25)[0], device_id=1)
        assert _exc(result) == ExcCodes.ILLEGAL_ADDRESS.value
        # FC15：单反馈线圈（多线圈批量写见缺陷复现文件 D5）
        assert _exc(await collector.write_coils(COIL_FB_0, [True], device_id=1)) is None
    finally:
        collector.close()

    dcems = await harness.client(UNAUTHORIZED_IP)
    try:
        result = await dcems.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=1)
        assert _exc(result) is None
        assert words_float(result.registers) == pytest.approx(8.0)
        result = await dcems.read_holding_registers(FEEDBACK_PAIR, count=1, device_id=1)
        assert result.registers[0] == 77
        result = await dcems.read_coils(COIL_FB_0, count=1, device_id=1)  # FC01
        assert result.bits[0] is True
    finally:
        dcems.close()


async def test_feedback_expires_after_max_age(harness) -> None:
    collector = await harness.client(COLLECTOR_IP)
    try:
        assert (
            _exc(await collector.write_registers(FEEDBACK_FLOAT, float_words(5.0), device_id=1))
            is None
        )
    finally:
        collector.close()
    reader = await harness.client(UNAUTHORIZED_IP)
    try:
        result = await reader.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=1)
        assert words_float(result.registers) == pytest.approx(5.0)
        await asyncio.sleep(0.8)
        result = await reader.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=1)
        assert _exc(result) == ExcCodes.DEVICE_FAILURE.value
    finally:
        reader.close()


# --- DC-EMS 写命令：FC16/FC15 转发 Commander --------------------------------


async def test_dcems_write_command_forwarded_to_commander(harness) -> None:
    dcems = await harness.client(COMMANDER_IP)
    try:
        assert (
            _exc(await dcems.write_registers(COMMAND_FLOAT, float_words(-3.25), device_id=5))
            is None
        )  # FC16
        assert _exc(await dcems.write_coils(COIL_CMD, [True], device_id=5)) is None  # FC15
        result = await dcems.read_holding_registers(COMMAND_FLOAT, count=2, device_id=5)
        assert words_float(result.registers) == pytest.approx(-3.25)
        result = await dcems.read_coils(COIL_CMD, count=1, device_id=5)
        assert result.bits[0] is True
    finally:
        dcems.close()

    calls = harness.commander.calls
    assert [(c.device_id, c.point_id, c.kind, c.value) for c in calls] == [
        ("phys5", "sp", "double_value", pytest.approx(-3.25)),
        ("phys5", "spb", "bool_value", True),
    ]
    assert len({c.command_id for c in calls}) == 2
    assert calls[0].timeout == pytest.approx(3.0)
    # journal 在成功后应清空
    assert json.loads(harness.journal_path.read_text()) == {}


# --- 12 个 Unit ID 地址隔离 -------------------------------------------------


async def test_unit_id_address_isolation_across_12_units(harness) -> None:
    async def write_unit(unit: int, value: float) -> None:
        client = await harness.client(COLLECTOR_IP)
        try:
            assert (
                _exc(
                    await client.write_registers(FEEDBACK_FLOAT, float_words(value), device_id=unit)
                )
                is None
            )
        finally:
            client.close()

    sentinels = [unit * 1.5 + 0.25 for unit in ALL_UNITS]
    await asyncio.gather(*(write_unit(unit, sentinels[unit - 1]) for unit in ALL_UNITS))

    async def read_unit(unit: int) -> tuple[int, float]:
        client = await harness.client(UNAUTHORIZED_IP)
        try:
            result = await client.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=unit)
            assert _exc(result) is None
            return unit, words_float(result.registers)
        finally:
            client.close()

    values = dict(await asyncio.gather(*(read_unit(u) for u in ALL_UNITS)))
    for unit in ALL_UNITS:
        assert values[unit] == pytest.approx(sentinels[unit - 1]), f"unit {unit} 串扰: {values}"


async def test_command_unit_isolation_physical_targets(harness) -> None:
    """不同 Unit 的命令点必须路由到各自物理目标。"""
    dcems = await harness.client(COMMANDER_IP)
    try:
        for unit in (1, 6, 12):
            result = await dcems.write_registers(
                COMMAND_FLOAT, float_words(float(unit)), device_id=unit
            )
            assert _exc(result) is None
    finally:
        dcems.close()
    seen = {c.device_id: c.value for c in harness.commander.calls}
    assert set(seen) == {"phys1", "phys6", "phys12"}
    assert seen["phys1"] == pytest.approx(1.0)
    assert seen["phys6"] == pytest.approx(6.0)
    assert seen["phys12"] == pytest.approx(12.0)


# --- 权限矩阵 ---------------------------------------------------------------


async def test_client_permission_matrix(harness) -> None:
    stranger = await harness.client(UNAUTHORIZED_IP)
    try:
        result = await stranger.write_registers(FEEDBACK_UINT, [1], device_id=1)
        assert _exc(result) == ExcCodes.ILLEGAL_ADDRESS.value
        result = await stranger.write_coils(COIL_FB_0, [True], device_id=1)
        assert _exc(result) == ExcCodes.ILLEGAL_ADDRESS.value
    finally:
        stranger.close()

    collector = await harness.client(COLLECTOR_IP)
    try:
        assert (
            _exc(await collector.write_registers(COMMAND_UINT, [1], device_id=1))
            == ExcCodes.ILLEGAL_ADDRESS.value
        )
        assert (
            _exc(await collector.write_coils(COIL_CMD, [True], device_id=1))
            == ExcCodes.ILLEGAL_ADDRESS.value
        )
        assert _exc(await collector.write_registers(FEEDBACK_UINT, [9], device_id=1)) is None
    finally:
        collector.close()

    dcems = await harness.client(COMMANDER_IP)
    try:
        assert (
            _exc(await dcems.write_registers(FEEDBACK_UINT, [1], device_id=1))
            == ExcCodes.ILLEGAL_ADDRESS.value
        )
        assert (
            _exc(await dcems.write_coils(COIL_FB_0, [True], device_id=1))
            == ExcCodes.ILLEGAL_ADDRESS.value
        )
        assert _exc(await dcems.write_registers(COMMAND_UINT, [9], device_id=1)) is None
    finally:
        dcems.close()


async def test_half_point_and_gap_writes_rejected(harness) -> None:
    collector = await harness.client(COLLECTOR_IP)
    try:
        # FC16 从 float32 映射点的第二个字开始
        result = await collector.write_registers(FEEDBACK_FLOAT + 1, [1, 2], device_id=1)
        assert _exc(result) == ExcCodes.ILLEGAL_ADDRESS.value
        # FC15 只写一个反馈线圈（允许）；多线圈见缺陷复现文件 D5
        assert _exc(await collector.write_coils(COIL_FB_0, [True], device_id=1)) is None
        # 空洞起始地址（120..199 无映射）
        result = await collector.write_registers(120, [1, 2], device_id=1)
        assert _exc(result) == ExcCodes.ILLEGAL_ADDRESS.value
    finally:
        collector.close()


async def test_unknown_unit_id_rejected(harness) -> None:
    collector = await harness.client(COLLECTOR_IP)
    reader = await harness.client(UNAUTHORIZED_IP)
    try:
        assert (
            _exc(await collector.write_registers(FEEDBACK_FLOAT, float_words(3.0), device_id=1))
            is None
        )
        for unit in (13, 100, 247):
            result = await collector.write_registers(
                FEEDBACK_FLOAT, float_words(99.0), device_id=unit
            )
            assert _exc(result) is not None, f"未配置 unit {unit} 不应被接受"
            result = await reader.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=unit)
            assert _exc(result) is not None
        # 未配置 unit 的拒绝不得落到 unit 1：unit 1 仍为原值
        result = await reader.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=1)
        assert words_float(result.registers) == pytest.approx(3.0)
    finally:
        collector.close()
        reader.close()


# --- Commander 三态与重复命令串行 ------------------------------------------


async def test_commander_confirmed_failure_does_not_lock(harness) -> None:
    harness.commander.set_outcome("phys1", "spu", pb.COMMAND_OUTCOME_CONFIRMED_FAILURE)
    dcems = await harness.client(COMMANDER_IP)
    try:
        result = await dcems.write_registers(COMMAND_UINT, [11], device_id=1)
        assert _exc(result) == ExcCodes.DEVICE_FAILURE.value
        harness.commander.set_outcome("phys1", "spu", pb.COMMAND_OUTCOME_CONFIRMED_SUCCESS)
        result = await dcems.write_registers(COMMAND_UINT, [12], device_id=1)
        assert _exc(result) is None
        readback = await dcems.read_holding_registers(COMMAND_UINT, count=1, device_id=1)
        assert readback.registers[0] == 12
    finally:
        dcems.close()
    assert len(harness.commander.calls) == 2


async def test_commander_unknown_locks_point_without_retry(harness) -> None:
    harness.commander.set_outcome("phys2", "spu", pb.COMMAND_OUTCOME_UNKNOWN)
    dcems = await harness.client(COMMANDER_IP)
    try:
        result = await dcems.write_registers(COMMAND_UINT, [21], device_id=2)
        assert _exc(result) == ExcCodes.DEVICE_FAILURE.value
        before = len(harness.commander.calls)
        for _ in range(3):
            result = await dcems.write_registers(COMMAND_UINT, [22], device_id=2)
            assert _exc(result) == ExcCodes.DEVICE_FAILURE.value
        assert len(harness.commander.calls) == before
    finally:
        dcems.close()
    # journal 中残留未决登记
    pending = json.loads(harness.journal_path.read_text())
    assert any(key.endswith('["turbine_002","cmd_uint"]') for key in pending)


async def test_unknown_fault_isolated_per_point_and_unit(harness) -> None:
    harness.commander.set_outcome("phys3", "spu", pb.COMMAND_OUTCOME_UNKNOWN)
    dcems = await harness.client(COMMANDER_IP)
    try:
        assert (
            _exc(await dcems.write_registers(COMMAND_UINT, [1], device_id=3))
            == ExcCodes.DEVICE_FAILURE.value
        )
        assert (
            _exc(await dcems.write_registers(COMMAND_UINT, [1], device_id=3))
            == ExcCodes.DEVICE_FAILURE.value
        )
        # 同设备其他命令点不受影响
        assert _exc(await dcems.write_coils(COIL_CMD, [True], device_id=3)) is None
        assert (
            _exc(await dcems.write_registers(COMMAND_FLOAT, float_words(1.0), device_id=3)) is None
        )
        # 其他 Unit 不受影响
        assert _exc(await dcems.write_registers(COMMAND_UINT, [2], device_id=4)) is None
    finally:
        dcems.close()
    # Collector 反馈链路不受影响
    collector = await harness.client(COLLECTOR_IP)
    try:
        assert _exc(await collector.write_registers(FEEDBACK_UINT, [31], device_id=3)) is None
    finally:
        collector.close()
    reader = await harness.client(UNAUTHORIZED_IP)
    try:
        result = await reader.read_holding_registers(FEEDBACK_UINT, count=1, device_id=3)
        assert result.registers[0] == 31
    finally:
        reader.close()


async def test_duplicate_concurrent_commands_serialize_per_point(harness) -> None:
    dcems = await harness.client(COMMANDER_IP)
    try:
        harness.commander.set_delay("phys7", "spu", 0.15)

        async def one(value: int) -> int | None:
            return _exc(await dcems.write_registers(COMMAND_UINT, [value], device_id=7))

        results = await asyncio.gather(*(one(100 + i) for i in range(6)))
    finally:
        dcems.close()
    assert results == [None] * 6
    spu_calls = [
        c for c in harness.commander.calls if (c.device_id, c.point_id) == ("phys7", "spu")
    ]
    assert len(spu_calls) == 6
    assert len({c.command_id for c in spu_calls}) == 6
    assert {c.value for c in spu_calls} == {100, 101, 102, 103, 104, 105}
    assert harness.commander.max_inflight("phys7", "spu") == 1


async def test_unknown_state_persists_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """journal 残留 UNKNOWN 命令：重启后保持闭锁，且不产生任何 RPC。"""
    install_server_shims(monkeypatch)
    journal = tmp_path / "commands.json"

    first_commander = FakeCommanderService()
    config = build_config(reserve_port(), reserve_port(), journal)
    runtime = await start_gateway(config, first_commander)
    dcems = await runtime.client(COMMANDER_IP)
    try:
        # 停止 Commander gRPC，制造 RPC 断开 -> UNKNOWN
        await runtime.grpc_server.stop(grace=0)
        result = await dcems.write_registers(COMMAND_UINT, [1], device_id=1)
        assert _exc(result) == ExcCodes.DEVICE_FAILURE.value
    finally:
        dcems.close()
        await runtime.stop()

    assert json.loads(journal.read_text()), "UNKNOWN 命令必须落盘"

    second_commander = FakeCommanderService()
    config2 = build_config(reserve_port(), reserve_port(), journal)
    runtime2 = await start_gateway(config2, second_commander)
    dcems2 = await runtime2.client(COMMANDER_IP)
    try:
        result = await dcems2.write_registers(COMMAND_UINT, [2], device_id=1)
        assert _exc(result) == ExcCodes.DEVICE_FAILURE.value
    finally:
        dcems2.close()
        await runtime2.stop()
    assert second_commander.calls == []  # 闭锁期间不得发出 RPC


# --- 并发串扰：Unit / 事务 / 身份 ------------------------------------------


async def test_concurrent_unit_and_identity_no_crosstalk(harness) -> None:
    rng = random.Random(20261021)
    rounds = 40

    async def collector_round(round_index: int) -> None:
        unit = rng.randrange(1, UNIT_COUNT + 1)
        client = await harness.client(COLLECTOR_IP)
        try:
            value = float(round_index * 100 + unit)
            assert (
                _exc(
                    await client.write_registers(FEEDBACK_FLOAT, float_words(value), device_id=unit)
                )
                is None
            )
        finally:
            client.close()

    async def stranger_round() -> None:
        unit = rng.randrange(1, UNIT_COUNT + 1)
        client = await harness.client(UNAUTHORIZED_IP)
        try:
            result = await client.write_registers(FEEDBACK_UINT, [1], device_id=unit)
            assert _exc(result) == ExcCodes.ILLEGAL_ADDRESS.value
        finally:
            client.close()

    await asyncio.gather(
        *(collector_round(i) for i in range(rounds)),
        *(stranger_round() for _ in range(rounds)),
    )

    # 第二轮：每个 Unit 一个唯一哨兵，全部并发写入；同时未授权方并发写
    sentinels = {unit: float(60000 + unit * 7) for unit in ALL_UNITS}

    async def final_write(unit: int) -> None:
        writer = await harness.client(COLLECTOR_IP)
        try:
            assert (
                _exc(
                    await writer.write_registers(
                        FEEDBACK_FLOAT, float_words(sentinels[unit]), device_id=unit
                    )
                )
                is None
            )
        finally:
            writer.close()

    await asyncio.gather(
        *(final_write(unit) for unit in ALL_UNITS),
        *(stranger_round() for _ in range(rounds)),
    )

    # 全部收敛后并发读：逐 Unit 比对，任何错配即 Unit 串扰
    async def final_read(unit: int) -> tuple[int, float]:
        reader = await harness.client(UNAUTHORIZED_IP)
        try:
            result = await reader.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=unit)
            assert _exc(result) is None
            return unit, words_float(result.registers)
        finally:
            reader.close()

    settled = dict(await asyncio.gather(*(final_read(u) for u in ALL_UNITS)))
    for unit in ALL_UNITS:
        assert settled[unit] == pytest.approx(
            sentinels[unit]
        ), f"并发收敛后 unit {unit} 值错配: {settled}"


# --- 出厂配置 ---------------------------------------------------------------


def test_shipped_config_loads() -> None:
    config = load_config(Path("configs/industrial_gateway.yaml"))
    assert config.control.enabled is False
    assert [dev.protocol_address.unit_id for dev in config.devices.values()] == [1, 2]
