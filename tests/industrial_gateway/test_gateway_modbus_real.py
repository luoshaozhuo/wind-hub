"""industrial_gateway 真实 Modbus TCP 链路补充验收（无垫片）。

真实 Gateway Server（pymodbus 3.15 simulator）+ 真实 pymodbus 客户端 +
真实 grpc.aio Commander，不安装任何测试期垫片，不 monkeypatch 生产对象。

本文件只保留 ``test_gateway_r2_*`` 系列未覆盖的角度：
- FC16 一次跨点（float32 + uint16）反馈写、FC06 反馈单字写回声、
  半点 / 空洞 holding 拒绝；
- 未配置 Unit ID 写入拒绝且不向已配置 Unit 串值；
- CONFIRMED_FAILURE 后同点可恢复（区别于 UNKNOWN 永久闭锁）；
- 40 轮随机 Unit/身份并发压力下无串扰；
- 出厂配置结构校验。

D1-D5 缺陷回归与流水线、三态、权限等主路径见同目录 ``test_gateway_r2_*``。
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from pymodbus.constants import ExcCodes

from contracts import commander_pb2 as pb
from industrial_gateway.infrastructure.config.loader import load_config
from tests.support.gateway_harness import (
    COLLECTOR_IP,
    COMMAND_UINT,
    COMMANDER_IP,
    FEEDBACK_FLOAT,
    FEEDBACK_PAIR,
    FEEDBACK_UINT,
    UNAUTHORIZED_IP,
    FakeCommanderService,
    build_config,
    float_words,
    reserve_port,
    start_gateway,
    words_float,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_service,
    pytest.mark.modbus,
    pytest.mark.fast,
]

UNIT_COUNT = 12
ALL_UNITS = list(range(1, UNIT_COUNT + 1))


@pytest.fixture
async def harness(tmp_path: Path):
    commander = FakeCommanderService()
    config = build_config(
        reserve_port(),
        reserve_port(),
        tmp_path / "commands.json",
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


async def test_fc16_span_pair_and_fc06_feedback_echo(harness) -> None:
    """FC16 跨相邻 float32+uint16 两个反馈点；FC06 反馈单字写回声一致。"""
    collector = await harness.client(COLLECTOR_IP)
    try:
        # FC16：一次覆盖相邻 float32(100-101) + uint16(102) 两个反馈点
        assert (
            _exc(
                await collector.write_registers(
                    FEEDBACK_FLOAT, float_words(8.0) + [77], device_id=1
                )
            )
            is None
        )
        # 单字写半个 float32 必须拒绝
        result = await collector.write_register(FEEDBACK_FLOAT, float_words(-40.25)[0], device_id=1)
        assert _exc(result) == ExcCodes.ILLEGAL_ADDRESS.value
        # FC06：uint16 反馈点单字写必须成功并可回读（旧 D3 分裂路径）
        result = await collector.write_register(FEEDBACK_UINT, 4242, device_id=1)
        assert _exc(result) is None
    finally:
        collector.close()

    reader = await harness.client(UNAUTHORIZED_IP)
    try:
        result = await reader.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=1)
        assert _exc(result) is None
        assert words_float(result.registers) == pytest.approx(8.0)
        result = await reader.read_holding_registers(FEEDBACK_PAIR, count=1, device_id=1)
        assert result.registers[0] == 77
        result = await reader.read_holding_registers(FEEDBACK_UINT, count=1, device_id=1)
        assert result.registers[0] == 4242
    finally:
        reader.close()


async def test_half_point_and_gap_holding_writes_rejected(harness) -> None:
    """holding 区半点起始与完全空洞的 FC16 写必须拒绝。"""
    collector = await harness.client(COLLECTOR_IP)
    try:
        # FC16 从 float32 映射点的第二个字开始
        result = await collector.write_registers(FEEDBACK_FLOAT + 1, [1, 2], device_id=1)
        assert _exc(result) == ExcCodes.ILLEGAL_ADDRESS.value
        # 空洞起始地址（120..199 无映射）
        result = await collector.write_registers(120, [1, 2], device_id=1)
        assert _exc(result) == ExcCodes.ILLEGAL_ADDRESS.value
    finally:
        collector.close()


async def test_unknown_unit_write_rejected_without_bleed(harness) -> None:
    """写未配置 Unit 必须拒绝，且不得落到同连接的已配置 Unit。"""
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


async def test_confirmed_failure_recovers_on_same_point(harness) -> None:
    """CONFIRMED_FAILURE 不闭锁：现场恢复后同一控制点可再次成功并更新镜像。"""
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


async def test_concurrent_unit_and_identity_no_crosstalk(harness) -> None:
    """40 轮随机 Unit 反馈写与未授权写并发，收敛后逐 Unit 哨兵值零错配。"""
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


def test_shipped_config_loads() -> None:
    config = load_config(Path("configs/industrial_gateway.yaml"))
    assert config.control.enabled is False
    assert [dev.protocol_address.unit_id for dev in config.devices.values()] == [1, 2]
