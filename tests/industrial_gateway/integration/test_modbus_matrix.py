"""真实 PyModbus TCP 客户端 ↔ 真实 Gateway v1 服务器 FC 矩阵。

每个用例都经过真实 TCP/Modbus 帧与生产 DeviceModbusServer，无 Mock：
- FC01/02/03/04 读、FC05/06/15/16 写、BOOL/INT16/UINT16/FLOAT32 与字序；
- 半点/空洞/非法值/未配置 Unit 的异常码；
- 控制点写触发真实 Commander 转发（含 FC05/06 回声读不得重复转发）；
- 断连重连、两台独立服务器隔离。
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest
from pymodbus.client import AsyncModbusTcpClient

from contracts import industrial_gateway_pb2 as pb
from core.infrastructure.rpc.scalar_codec import encode_scalar
from tests.support.gateway_v1 import (
    COMMAND_COIL,
    COMMAND_COIL_B,
    COMMAND_FLOAT,
    COMMAND_UINT,
    COMMAND_UINT2,
    FEEDBACK_COIL_0,
    FEEDBACK_COIL_1,
    FEEDBACK_COIL_15,
    FEEDBACK_COIL_16,
    FEEDBACK_COIL_17,
    FEEDBACK_DISCRETE,
    FEEDBACK_FLOAT,
    FEEDBACK_INPUT,
    FEEDBACK_PAIR,
    FEEDBACK_UINT,
    POINT_TABLE,
    float_words,
    start_gateway_v1,
    two_server_specs,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_service,
    pytest.mark.modbus,
    pytest.mark.fast,
]

# 点名 → 原始 Modbus 地址（bit FC 用位地址，字 FC 用寄存器地址）。
ADDR: dict[str, int] = {name: spec["address"]["address"] for name, spec in POINT_TABLE.items()}


def _exc(response: object) -> int | None:
    if not response.isError():
        return None
    code = response.exception_code
    return int(code.value) if hasattr(code, "value") else int(code)


async def _client(harness, server_id: str) -> AsyncModbusTcpClient:
    client = harness.modbus_client(server_id)
    await client.connect()
    assert client.connected
    return client


async def _seed(harness, server_id: str, values: dict[str, object]) -> None:
    """通过真实 gRPC UpdateVariables 注入反馈值（Generator 通道）。"""
    stub = harness.gateway_stub()
    reply = await stub.UpdateVariables(
        pb.UpdateVariablesRequest(
            gateway_id=server_id,
            values=[
                pb.VariableUpdate(variable_id=name, value=encode_scalar(value))
                for name, value in values.items()
            ],
        )
    )
    failed = [item for item in reply.results if not item.success]
    assert failed == [], failed


@pytest.fixture
async def harness(tmp_path: Path):
    gateway = await start_gateway_v1(tmp_path / "root", two_server_specs())
    try:
        yield gateway
    finally:
        await gateway.stop()


# ---------- FC03/04 读与字序 ----------


async def test_fc03_holding_reads_float_and_uint_in_big_endian(harness) -> None:
    await _seed(harness, "gw1", {FEEDBACK_FLOAT: 42.5, FEEDBACK_UINT: 4242})
    client = await _client(harness, "gw1")
    try:
        read = await client.read_holding_registers(ADDR[FEEDBACK_FLOAT], count=2, device_id=1)
        assert _exc(read) is None
        assert read.registers == float_words(42.5)
        uint_read = await client.read_holding_registers(ADDR[FEEDBACK_UINT], count=1, device_id=1)
        assert uint_read.registers == [4242]
    finally:
        client.close()


async def test_fc03_uninitialized_holding_reads_zero(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        read = await client.read_holding_registers(ADDR[FEEDBACK_FLOAT], count=2, device_id=1)
        assert _exc(read) is None
        assert read.registers == [0, 0]
    finally:
        client.close()


async def test_fc04_input_int16_signed_decoding(harness) -> None:
    await _seed(harness, "gw1", {FEEDBACK_INPUT: -5})
    client = await _client(harness, "gw1")
    try:
        read = await client.read_input_registers(ADDR[FEEDBACK_INPUT], count=1, device_id=1)
        assert _exc(read) is None
        # 原始 16-bit 无符号字，符号语义由点表 int16 决定，线上即二补码 0xFFFB。
        assert read.registers == [(-5) & 0xFFFF]
    finally:
        client.close()


# ---------- FC01/02 位读 ----------


async def test_fc01_coils_and_fc02_discrete_reads(harness) -> None:
    await _seed(
        harness,
        "gw1",
        {
            FEEDBACK_COIL_0: False,
            FEEDBACK_COIL_1: True,
            FEEDBACK_COIL_15: True,
            FEEDBACK_COIL_16: False,
            FEEDBACK_COIL_17: True,
            FEEDBACK_DISCRETE: True,
        },
    )
    client = await _client(harness, "gw1")
    try:
        coils = await client.read_coils(ADDR[FEEDBACK_COIL_0], count=18, device_id=1)
        assert _exc(coils) is None
        assert coils.bits[:18] == [
            False,
            True,
            *[False] * 13,
            True,
            False,
            True,
        ]
        discrete = await client.read_discrete_inputs(ADDR[FEEDBACK_DISCRETE], count=1, device_id=1)
        assert _exc(discrete) is None
        assert discrete.bits[0] is True
    finally:
        client.close()


# ---------- FC05/06/15/16 写 ----------


async def test_fc05_single_coil_write_updates_store_and_reads_back(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        result = await client.write_coil(ADDR[FEEDBACK_COIL_0], True, device_id=1)
        assert _exc(result) is None, result
        again = await client.write_coil(ADDR[FEEDBACK_COIL_0], False, device_id=1)
        assert _exc(again) is None
        read = await client.read_coils(ADDR[FEEDBACK_COIL_0], count=1, device_id=1)
        assert read.bits[0] is False
    finally:
        client.close()
    read = await harness.gateway_stub().ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_COIL_0])
    )
    assert read.values[0].value.bool_value is False


async def test_fc15_multiple_coils_crosses_word_boundary(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        result = await client.write_coils(ADDR[FEEDBACK_COIL_15], [True, False, True], device_id=1)
        assert _exc(result) is None, result
        read = await client.read_coils(ADDR[FEEDBACK_COIL_15], count=3, device_id=1)
        assert read.bits[:3] == [True, False, True]
    finally:
        client.close()


async def test_fc06_single_holding_register_write(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        result = await client.write_register(ADDR[FEEDBACK_UINT], 4242, device_id=1)
        assert _exc(result) is None, result
        read = await client.read_holding_registers(ADDR[FEEDBACK_UINT], count=1, device_id=1)
        assert read.registers == [4242]
    finally:
        client.close()


async def test_fc16_float32_write_big_endian(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        result = await client.write_registers(
            ADDR[FEEDBACK_FLOAT], float_words(-40.25), device_id=1
        )
        assert _exc(result) is None, result
        read = await client.read_holding_registers(ADDR[FEEDBACK_FLOAT], count=2, device_id=1)
        assert read.registers == float_words(-40.25)
    finally:
        client.close()
    read = await harness.gateway_stub().ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_FLOAT])
    )
    assert abs(read.values[0].value.double_value - (-40.25)) < 1e-6


async def test_fc16_spans_adjacent_feedback_points(harness) -> None:
    """FC16 一次覆盖相邻 float32(100-101)+uint16(102) 两个反馈点。"""
    client = await _client(harness, "gw1")
    try:
        result = await client.write_registers(
            ADDR[FEEDBACK_FLOAT], float_words(8.0) + [77], device_id=1
        )
        assert _exc(result) is None, result
        read = await client.read_holding_registers(ADDR[FEEDBACK_PAIR], count=1, device_id=1)
        assert read.registers == [77]
    finally:
        client.close()


# ---------- 非法地址与非法值 ----------


async def test_half_point_and_gap_holding_writes_rejected(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        # float32 的第二个字起始：半点，拒绝，不产生部分写入。
        half = await client.write_registers(ADDR[FEEDBACK_FLOAT] + 1, [1, 2], device_id=1)
        assert _exc(half) == 0x02
        gap = await client.write_registers(120, [1, 2], device_id=1)
        assert _exc(gap) == 0x02
        read = await client.read_holding_registers(ADDR[FEEDBACK_FLOAT], count=2, device_id=1)
        assert read.registers == [0, 0]
    finally:
        client.close()


async def test_half_point_holding_read_rejected(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        half = await client.read_holding_registers(ADDR[FEEDBACK_FLOAT] + 1, count=1, device_id=1)
        assert _exc(half) == 0x02
    finally:
        client.close()


async def test_unmapped_coil_write_and_partial_span_rejected(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        hole = await client.write_coil(5, True, device_id=1)
        assert _exc(hole) == 0x02
        # 0/1 映射、2 空洞：跨出映射的批量写整体拒绝。
        seeded = await client.write_coils(ADDR[FEEDBACK_COIL_0], [True, True], device_id=1)
        assert _exc(seeded) is None
        rejected = await client.write_coils(
            ADDR[FEEDBACK_COIL_0], [False, False, False], device_id=1
        )
        assert _exc(rejected) == 0x02
        read = await client.read_coils(ADDR[FEEDBACK_COIL_0], count=2, device_id=1)
        assert read.bits[:2] == [True, True]  # 拒绝后无部分改写
    finally:
        client.close()


async def test_fc16_nan_float32_rejected_as_illegal_value(harness) -> None:
    """float32 非有限值可在线上编码但必须被点类型校验拒绝（0x03）。"""
    client = await _client(harness, "gw1")
    try:
        nan_words = float_words(math.nan)
        result = await client.write_registers(ADDR[FEEDBACK_FLOAT], nan_words, device_id=1)
        assert _exc(result) == 0x03
    finally:
        client.close()


async def test_unknown_unit_id_rejected_without_bleed(harness) -> None:
    """未配置 Unit 必须拒绝（pymodbus NoSuchId → 0x0B 路径），且不得落到 unit 1。"""
    client = await _client(harness, "gw1")
    try:
        good = await client.write_registers(ADDR[FEEDBACK_FLOAT], float_words(3.0), device_id=1)
        assert _exc(good) is None
        failures = []
        for unit in (2, 13, 100, 247):
            result = await client.write_registers(
                ADDR[FEEDBACK_FLOAT], float_words(99.0), device_id=unit
            )
            failures.append(_exc(result))
        assert all(code is not None for code in failures), failures
        read = await client.read_holding_registers(ADDR[FEEDBACK_FLOAT], count=2, device_id=1)
        assert read.registers == float_words(3.0)
    finally:
        client.close()


# ---------- 控制点写 → Commander 转发 ----------


async def _wait_calls(harness, count: int, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if len(harness.commander.calls) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"expected {count} commander calls, got {harness.commander.calls}")


async def test_fc06_command_write_forwards_once_not_twice_for_echo_read(
    harness,
) -> None:
    """FC06 在 pymodbus 3.15 写完后会回声读一次 action；只允许一次转发。"""
    client = await _client(harness, "gw1")
    try:
        result = await client.write_register(ADDR[COMMAND_UINT], 11, device_id=1)
        assert _exc(result) is None, result
    finally:
        client.close()
    await _wait_calls(harness, 1)
    calls = [c for c in harness.commander.calls if c.device_id == "D01"]
    assert len(calls) == 1
    assert calls[0].point_id == COMMAND_UINT
    assert calls[0].value == 11
    assert calls[0].revision == "rev-D01"  # 提交时取 Commander revision


async def test_same_value_command_write_twice_submits_twice(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        for _ in range(2):
            result = await client.write_register(ADDR[COMMAND_UINT], 7, device_id=1)
            assert _exc(result) is None
    finally:
        client.close()
    await _wait_calls(harness, 2)
    calls = [c for c in harness.commander.calls if c.point_id == COMMAND_UINT]
    assert len(calls) == 2
    assert calls[0].command_id != calls[1].command_id


async def test_fc05_command_coil_write_forwards_bool(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        result = await client.write_coil(ADDR[COMMAND_COIL], True, device_id=1)
        assert _exc(result) is None
    finally:
        client.close()
    await _wait_calls(harness, 1)
    assert [call for call in harness.commander.calls if call.device_id == "D01"][0].value is True


async def test_fc16_command_float_write_forwards_float(harness) -> None:
    client = await _client(harness, "gw1")
    try:
        result = await client.write_registers(ADDR[COMMAND_FLOAT], float_words(12.5), device_id=1)
        assert _exc(result) is None
    finally:
        client.close()
    await _wait_calls(harness, 1)
    call = next(c for c in harness.commander.calls if c.point_id == COMMAND_FLOAT)
    assert abs(call.value - 12.5) < 1e-6


async def test_fc15_span_two_command_points_stores_and_forwards_each(
    harness,
) -> None:
    """v1 实际行为：一次 FC15 跨 cmd_coil(32)/cmd_coil_b(33) 被接受，
    两点都落存储且各转发一次。旧 v2 设计是整体拒绝零转发；
    此差异已在验收报告中提交设计方确认（待同步设计版本差异）。"""
    client = await _client(harness, "gw1")
    try:
        result = await client.write_coils(ADDR[COMMAND_COIL], [True, False], device_id=1)
        assert _exc(result) is None, result
        read = await client.read_coils(ADDR[COMMAND_COIL], count=2, device_id=1)
        assert read.bits[:2] == [True, False]
    finally:
        client.close()
    await _wait_calls(harness, 2)
    points = {call.point_id for call in harness.commander.calls if call.device_id == "D01"}
    assert points == {COMMAND_COIL, COMMAND_COIL_B}


async def test_fc16_span_two_command_register_points_forwards_each(harness) -> None:
    """同 FC15：跨 cmd_float(200-201)+cmd_uint2(202) 的 FC16 被接受并各转发。"""
    client = await _client(harness, "gw1")
    try:
        result = await client.write_registers(
            ADDR[COMMAND_FLOAT], float_words(1.0) + [5], device_id=1
        )
        assert _exc(result) is None
    finally:
        client.close()
    await _wait_calls(harness, 2)
    calls = {c.point_id: c.value for c in harness.commander.calls if c.device_id == "D01"}
    assert set(calls) == {COMMAND_FLOAT, COMMAND_UINT2}
    assert calls[COMMAND_UINT2] == 5
    assert calls[COMMAND_FLOAT] == pytest.approx(1.0)


async def test_control_disabled_command_write_stores_but_never_forwards(
    tmp_path: Path,
) -> None:
    specs = two_server_specs(control_enabled=False)
    gateway = await start_gateway_v1(tmp_path / "root", specs)
    try:
        client = gateway.modbus_client("gw1")
        await client.connect()
        try:
            result = await client.write_register(ADDR[COMMAND_UINT], 9, device_id=1)
            assert _exc(result) is None
        finally:
            client.close()
        await asyncio.sleep(0.3)  # 若有错误转发会在此窗口内落账
        assert gateway.commander.calls == []
    finally:
        await gateway.stop()


# ---------- 反馈点不转发、连接与多服务器隔离 ----------


async def test_feedback_write_does_not_forward(harness) -> None:
    """反馈点（无 command_mappings）写只更新存储，不产生 Commander 提交。"""
    client = await _client(harness, "gw1")
    try:
        result = await client.write_register(ADDR[FEEDBACK_UINT], 5, device_id=1)
        assert _exc(result) is None
        result = await client.write_registers(ADDR[FEEDBACK_FLOAT], float_words(6.0), device_id=1)
        assert _exc(result) is None
    finally:
        client.close()
    await asyncio.sleep(0.3)
    assert harness.commander.calls == []


async def test_disconnect_and_reconnect_keeps_store(harness) -> None:
    await _seed(harness, "gw1", {FEEDBACK_UINT: 314})
    first = await _client(harness, "gw1")
    first.close()
    assert not first.connected
    second = await _client(harness, "gw1")
    try:
        read = await second.read_holding_registers(ADDR[FEEDBACK_UINT], count=1, device_id=1)
        assert read.registers == [314]
    finally:
        second.close()


async def test_two_servers_are_independent_listeners(harness) -> None:
    c1 = await _client(harness, "gw1")
    c2 = await _client(harness, "gw2")
    try:
        r1 = await c1.write_registers(ADDR[FEEDBACK_FLOAT], float_words(1.5), device_id=1)
        r2 = await c2.write_registers(ADDR[FEEDBACK_FLOAT], float_words(2.5), device_id=2)
        assert _exc(r1) is None and _exc(r2) is None
        read1 = await c1.read_holding_registers(ADDR[FEEDBACK_FLOAT], count=2, device_id=1)
        read2 = await c2.read_holding_registers(ADDR[FEEDBACK_FLOAT], count=2, device_id=2)
        assert read1.registers == float_words(1.5)
        assert read2.registers == float_words(2.5)
    finally:
        c1.close()
        c2.close()
