"""真实 Modbus 控制写 → 真实 Commander 转发终态端到端契约。

v1 转发是尽力而为内存模型：Modbus 写回调只做入队（capture 不 await），
Commander 裁决从不同步反映到 Modbus 响应；终态只通过 forwarder 记录与
GetCommandResult 外显。本文件钉住该模型下的关键语义：
- REJECTED/UNKNOWN 不闭锁同点后续新事件，无自动重试；
- UNKNOWN 不得呈现为 ACCEPTED；
- 失败按点/按设备隔离；
- 多连接同点并发在 Commander 侧严格串行（max_inflight=1）；
- 在途提交不阻塞协议读；客户端断连不取消在途提交。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from contracts import industrial_gateway_pb2 as pb
from industrial_gateway.infrastructure.adapters.outbound.forwarder import ForwardState
from tests.support.gateway_v1 import (
    COMMAND_COIL,
    COMMAND_UINT,
    FEEDBACK_FLOAT,
    POINT_TABLE,
    start_gateway_v1,
    two_server_specs,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_service,
    pytest.mark.modbus,
    pytest.mark.fast,
]

ADDR: dict[str, int] = {name: spec["address"]["address"] for name, spec in POINT_TABLE.items()}


def _exc(response: object) -> int | None:
    if not response.isError():
        return None
    code = response.exception_code
    return int(code.value) if hasattr(code, "value") else int(code)


def _all_records(harness) -> list:
    # 每台 Server 独立持有一个转发器（forwarders 按 gateway_id 键控）。
    return [
        record for forwarder in harness.runtime.forwarders.values() for record in forwarder.records
    ]


async def _records_for(harness, device: str, point: str, *, timeout: float = 3.0) -> list:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        matched = [
            record
            for record in _all_records(harness)
            if record.device_id == device and record.point_id == point
        ]
        if matched and all(
            record.state
            not in {
                ForwardState.QUEUED,
                ForwardState.VALIDATING,
                ForwardState.SUBMITTING,
            }
            for record in matched
        ):
            return matched
        await asyncio.sleep(0.02)
    raise AssertionError("forward records did not terminate")


async def _write_command(harness, server: str, address: int, value: int) -> None:
    client = harness.modbus_client(server)
    await client.connect()
    try:
        result = await client.write_register(address, value, device_id=1)
        assert _exc(result) is None, result
    finally:
        client.close()


@pytest.fixture
async def harness(tmp_path: Path):
    gateway = await start_gateway_v1(tmp_path / "root", two_server_specs())
    try:
        yield gateway
    finally:
        await gateway.stop()


async def test_commander_rejected_is_terminal_but_does_not_latch(harness) -> None:
    harness.commander.set_rejected("D01", COMMAND_UINT, message="interlock open")
    await _write_command(harness, "gw1", ADDR[COMMAND_UINT], 11)
    records = await _records_for(harness, "D01", COMMAND_UINT)
    assert len(records) == 1
    assert records[0].state is ForwardState.REJECTED
    assert "interlock open" in records[0].error
    await asyncio.sleep(0.2)
    assert len(harness.commander.calls) == 1  # 无自动重试

    # 恢复后同点新事件正常提交：REJECTED 不闭锁（无 v2 持久 latch）。
    harness.commander.accepted[("D01", COMMAND_UINT)] = True
    harness.commander.messages.pop(("D01", COMMAND_UINT), None)
    await _write_command(harness, "gw1", ADDR[COMMAND_UINT], 12)
    records = await _records_for(harness, "D01", COMMAND_UINT)
    assert len(records) == 2
    assert records[-1].state is ForwardState.ACCEPTED
    assert len(harness.commander.calls) == 2


async def test_commander_unavailable_is_unknown_never_accepted(harness) -> None:
    import grpc

    harness.commander.rpc_aborts[("D01", COMMAND_UINT)] = grpc.StatusCode.UNAVAILABLE
    await _write_command(harness, "gw1", ADDR[COMMAND_UINT], 21)
    records = await _records_for(harness, "D01", COMMAND_UINT)
    assert len(records) == 1
    assert records[0].state is ForwardState.UNKNOWN
    assert not records[0].command_id  # 无权威 command_id 不得伪装成功
    # 存储侧确实收到值；UNKNOWN 的歧义在记录而非 Modbus 响应上诚实暴露。
    read = await harness.gateway_stub().ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[COMMAND_UINT])
    )
    assert read.values[0].value.sint64_value == 21


async def test_failure_isolates_by_point_and_device(harness) -> None:
    harness.commander.set_rejected("D01", COMMAND_UINT)
    await _write_command(harness, "gw1", ADDR[COMMAND_UINT], 1)
    # 同设备另一点（线圈）仍 ACCEPTED。
    coil_client = harness.modbus_client("gw1")
    await coil_client.connect()
    try:
        result = await coil_client.write_coil(ADDR[COMMAND_COIL], True, device_id=1)
        assert _exc(result) is None
    finally:
        coil_client.close()
    # 另一设备的同名点不受影响。
    d2 = harness.modbus_client("gw2")
    await d2.connect()
    try:
        result = await d2.write_register(ADDR[COMMAND_UINT], 2, device_id=2)
        assert _exc(result) is None
    finally:
        d2.close()

    rejected = await _records_for(harness, "D01", COMMAND_UINT)
    assert rejected[0].state is ForwardState.REJECTED
    coil_records = await _records_for(harness, "D01", COMMAND_COIL)
    assert coil_records[0].state is ForwardState.ACCEPTED
    d2_records = await _records_for(harness, "D02", COMMAND_UINT)
    assert d2_records[0].state is ForwardState.ACCEPTED


async def test_concurrent_same_point_writes_serialize_at_commander(harness) -> None:
    """4 连接并发写同一控制点：Commander 侧在途恒为 1，4 个不同 command_id。"""
    harness.commander.set_delay("D01", COMMAND_UINT, 0.15)

    async def one(value: int) -> None:
        client = harness.modbus_client("gw1")
        await client.connect()
        try:
            result = await client.write_register(ADDR[COMMAND_UINT], value, device_id=1)
            assert _exc(result) is None
        finally:
            client.close()

    await asyncio.gather(*(one(value) for value in (10, 20, 30, 40)))
    records = await _records_for(harness, "D01", COMMAND_UINT)
    calls = [c for c in harness.commander.calls if c.point_id == COMMAND_UINT]
    assert len(calls) == 4
    assert harness.commander.max_inflight("D01", COMMAND_UINT) == 1
    assert len({c.command_id for c in calls}) == 4
    # FIFO 锚点是 Gateway 捕获顺序（forwarder 记录插入序），不是四条 TCP 连接
    # 的建立顺序——后者受事件循环调度影响而不确定。按 command_id 联接后，
    # Commander 受理值序列必须与捕获值序列严格一致。
    captured = [record for record in records if record.command_id]
    assert len(captured) == 4  # 全部 ACCEPTED 且各有权威 command_id
    calls_by_id = {call.command_id: call for call in calls}
    assert {call.command_id for call in calls} == {record.command_id for record in captured}
    accepted_values = [calls_by_id[record.command_id].value for record in captured]
    assert accepted_values == [record.value for record in captured]  # 捕获序即受理序


async def test_read_succeeds_while_submission_in_flight(harness) -> None:
    """在途 gRPC 提交不得阻塞 Modbus 数据面：写后立即读反馈点快速返回。"""
    harness.commander.set_delay("D01", COMMAND_UINT, 0.8)
    writer = harness.modbus_client("gw1")
    await writer.connect()
    try:
        start = asyncio.get_running_loop().time()
        result = await writer.write_register(ADDR[COMMAND_UINT], 77, device_id=1)
        assert _exc(result) is None
        # 控制写已入队但提交在途；另一连接读反馈点必须立刻成功。
        reader = harness.modbus_client("gw1")
        await reader.connect()
        try:
            read = await reader.read_holding_registers(ADDR[FEEDBACK_FLOAT], count=2, device_id=1)
            assert _exc(read) is None
        finally:
            reader.close()
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed < 0.5
    finally:
        writer.close()
    pending = [record for record in _all_records(harness) if record.point_id == COMMAND_UINT]
    assert pending and pending[-1].state in {
        ForwardState.SUBMITTING,
        ForwardState.ACCEPTED,
    }


async def test_client_disconnect_does_not_cancel_in_flight_submission(
    harness,
) -> None:
    """客户端在提交在途期间断连：提交继续完成，不产生 UNKNOWN 假故障。"""
    harness.commander.set_delay("D01", COMMAND_UINT, 0.5)
    client = harness.modbus_client("gw1")
    await client.connect()
    try:
        result = await client.write_register(ADDR[COMMAND_UINT], 88, device_id=1)
        assert _exc(result) is None
    finally:
        client.close()  # 响应已返回即断连；转发与该连接无任务归属
    records = await _records_for(harness, "D01", COMMAND_UINT)
    assert records[0].state is ForwardState.ACCEPTED
