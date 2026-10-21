"""D4 回归：TCP 流水线、分帧、并发事务与队列边界（核心项）。

使用裸 asyncio socket 手工切分 TCP 段，pymodbus 客户端做不到这些控制。
每条合法请求必须有且仅有一个响应，TID/Unit/功能码/数据严格对应。
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest

from core.infrastructure.rpc import commander_pb2 as pb
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

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


def mbap(tid: int, unit: int, pdu: bytes) -> bytes:
    length = len(pdu) + 1
    return struct.pack(">HHHB", tid, 0, length, unit) + pdu


def fc16(start: int, words: list[int]) -> bytes:
    pdu = struct.pack(">BHHB", 0x10, start, len(words), len(words) * 2)
    pdu += b"".join(struct.pack(">H", w) for w in words)
    return pdu


def fc03(start: int, count: int) -> bytes:
    return struct.pack(">BHH", 0x03, start, count)


def fc06(start: int, value: int) -> bytes:
    return struct.pack(">BHH", 0x06, start, value)


async def raw_connect(port: int, source_ip: str):
    reader, writer = await asyncio.open_connection("127.0.0.1", port, local_addr=(source_ip, 0))
    return reader, writer


async def read_response(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    header = await reader.readexactly(7)
    tid, _proto, length, unit = struct.unpack(">HHHB", header)
    body = await reader.readexactly(length - 1)
    return tid, unit, body


@pytest.fixture
async def harness(tmp_path: Path):
    commander = FakeCommanderService()
    config = build_config(
        reserve_port(),
        reserve_port(),
        tmp_path / "journal.json",
        command_timeout=2.0,
        rpc_timeout=3.0,
    )
    gateway = await start_gateway(config, commander)
    try:
        yield gateway
    finally:
        await gateway.stop()


async def test_two_complete_requests_in_one_segment(harness) -> None:
    reader, writer = await raw_connect(harness.modbus_port, COLLECTOR_IP)
    try:
        blob = mbap(10, 1, fc16(FEEDBACK_FLOAT, float_words(1.0)))
        blob += mbap(20, 2, fc16(FEEDBACK_FLOAT, float_words(2.0)))
        writer.write(blob)
        r1 = await read_response(reader)
        r2 = await read_response(reader)
        assert (r1[0], r1[1], r1[2][0]) == (10, 1, 0x10)
        assert (r2[0], r2[1], r2[2][0]) == (20, 2, 0x10)
    finally:
        writer.close()

    dc = await harness.client(COMMANDER_IP)
    try:
        for unit, expected in ((1, 1.0), (2, 2.0)):
            read = await dc.read_holding_registers(FEEDBACK_FLOAT, count=2, device_id=unit)
            assert not read.isError() and read.registers == float_words(expected)
    finally:
        dc.close()


async def test_single_request_split_across_segments(harness) -> None:
    reader, writer = await raw_connect(harness.modbus_port, COLLECTOR_IP)
    try:
        frame = mbap(30, 3, fc16(FEEDBACK_FLOAT, float_words(3.5)))
        writer.write(frame[:4])
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.write(frame[4:])
        tid, unit, body = await read_response(reader)
        assert (tid, unit, body[0]) == (30, 3, 0x10)
    finally:
        writer.close()


async def test_half_packet_then_two_full_requests(harness) -> None:
    """半包与多个完整请求在同一/后续段混合到达。"""
    reader, writer = await raw_connect(harness.modbus_port, COLLECTOR_IP)
    try:
        frame_a = mbap(40, 4, fc16(FEEDBACK_FLOAT, float_words(4.25)))
        frame_b = mbap(41, 5, fc16(FEEDBACK_FLOAT, float_words(5.25)))
        writer.write(frame_a[:6])
        await writer.drain()
        await asyncio.sleep(0.15)
        writer.write(frame_a[6:] + frame_b)
        tid_a, unit_a, body_a = await read_response(reader)
        tid_b, unit_b, body_b = await read_response(reader)
        assert (tid_a, unit_a) == (40, 4)
        assert (tid_b, unit_b) == (41, 5)
    finally:
        writer.close()


async def test_interleaved_tid_and_units_one_segment(harness) -> None:
    # 先写入差异化数据
    dc = await harness.client(COLLECTOR_IP)
    try:
        for unit in range(1, 6):
            await dc.write_registers(
                FEEDBACK_FLOAT, float_words(float(unit) * 10.0), device_id=unit
            )
    finally:
        dc.close()

    reader, writer = await raw_connect(harness.modbus_port, COMMANDER_IP)
    try:
        blob = b"".join(mbap(500 + unit, unit, fc03(FEEDBACK_FLOAT, 2)) for unit in (5, 3, 1, 4, 2))
        writer.write(blob)
        seen: dict[int, tuple[int, bytes]] = {}
        for _ in range(5):
            tid, unit, body = await read_response(reader)
            seen[tid] = (unit, body)
        for unit in (5, 3, 1, 4, 2):
            tid = 500 + unit
            resp_unit, body = seen[tid]
            assert resp_unit == unit
            assert body[0] == 0x03 and body[1] == 4
            words = list(struct.unpack(">HH", body[2:6]))
            assert words == float_words(float(unit) * 10.0)
    finally:
        writer.close()


async def test_request_arrives_during_pending_grpc(harness) -> None:
    """Commander gRPC 等待期间同连接新请求排队，响应严格各归其主。"""
    commander: FakeCommanderService = harness.commander
    commander.set_delay("phys1", "spu", 0.6)
    # 预填 Unit2 反馈，使排队期间插入的读请求有确定的预期响应。
    collector = await harness.client(COLLECTOR_IP)
    try:
        seeded = await collector.write_registers(FEEDBACK_FLOAT, float_words(77.0), device_id=2)
        assert not seeded.isError()
    finally:
        collector.close()
    reader, writer = await raw_connect(harness.modbus_port, COMMANDER_IP)
    try:
        writer.write(mbap(60, 1, fc06(210, 909)))
        await writer.drain()
        await asyncio.sleep(0.15)
        # 控制命令仍在 gRPC 等待中；插入一个 Unit2 的读请求。
        writer.write(mbap(61, 2, fc03(FEEDBACK_FLOAT, 2)))
        responses = []
        for _ in range(2):
            responses.append(await read_response(reader))
        by_tid = {tid: (unit, body) for tid, unit, body in responses}
        assert by_tid[60][0] == 1 and by_tid[60][1][0] == 0x06
        assert by_tid[61][0] == 2 and by_tid[61][1][0] == 0x03
        assert list(struct.unpack(">HH", by_tid[61][1][2:6])) == float_words(77.0)
        assert len(commander.calls) == 1
    finally:
        writer.close()


async def test_multi_connection_concurrency(harness) -> None:
    """4 条连接、每条 8 个交错事务；每请求恰好一个对应响应。"""

    async def worker(conn_index: int) -> None:
        reader, writer = await raw_connect(harness.modbus_port, COLLECTOR_IP)
        try:
            blob = b""
            unit = conn_index + 1
            for seq in range(8):
                tid = 700 + conn_index * 100 + seq
                blob += mbap(tid, unit, fc16(FEEDBACK_FLOAT, float_words(seq + 0.5)))
            writer.write(blob)
            for seq in range(8):
                tid, resp_unit, body = await read_response(reader)
                assert tid == 700 + conn_index * 100 + seq
                assert resp_unit == unit and body[0] == 0x10
        finally:
            writer.close()

    await asyncio.gather(*(worker(i) for i in range(4)))


async def test_pipelined_command_pdus_do_not_overwrite(harness) -> None:
    """一段内多个不同 Unit/数值的 FC06：排队执行不得发生 last_pdu 覆盖串扰。"""
    commander: FakeCommanderService = harness.commander
    for unit in range(1, 5):
        commander.set_delay(f"phys{unit}", "spu", 0.2)
    reader, writer = await raw_connect(harness.modbus_port, COMMANDER_IP)
    try:
        values = {unit: 1000 + unit for unit in range(1, 5)}
        writer.write(
            b"".join(mbap(1200 + unit, unit, fc06(210, values[unit])) for unit in range(1, 5))
        )
        responses = {}
        for _ in range(4):
            tid, resp_unit, body = await read_response(reader)
            responses[tid] = (resp_unit, body)
        for unit in range(1, 5):
            resp_unit, body = responses[1200 + unit]
            assert resp_unit == unit
            assert body == fc06(210, values[unit])  # 回声的数值必须逐帧对应
    finally:
        writer.close()

    calls = sorted((c.device_id, c.value) for c in commander.calls if c.point_id == "spu")
    assert calls == [(f"phys{u}", 1000 + u) for u in range(1, 5)]
    assert len({c.command_id for c in commander.calls}) == 4


async def test_queue_overflow_closes_connection_and_bounds_execution(harness) -> None:
    """队列容量边界：第 65 个在途请求触发关连接；Commander 调用不超过 64。"""
    commander: FakeCommanderService = harness.commander
    for unit in range(1, 13):
        commander.set_delay(f"phys{unit}", "spu", 0.8)
    reader, writer = await raw_connect(harness.modbus_port, COMMANDER_IP)
    try:
        # 80 个 FC06，轮流 Unit，使同一控制点最多被命中 7 次。
        blob = b"".join(mbap(800 + seq, (seq % 12) + 1, fc06(210, seq % 1000)) for seq in range(80))
        writer.write(blob)
        await writer.drain()
        # 服务端必须主动关闭连接。
        with pytest.raises(asyncio.IncompleteReadError):
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    raise asyncio.IncompleteReadError(b"", None)
    finally:
        writer.close()
    # 等待在途命令全部结束。
    await asyncio.sleep(1.2)
    assert len(commander.calls) <= 64, len(commander.calls)
    ids = [c.command_id for c in commander.calls]
    assert len(ids) == len(set(ids))


async def test_malformed_length_closes_connection(harness) -> None:
    reader, writer = await raw_connect(harness.modbus_port, COMMANDER_IP)
    try:
        # MBAP length=0 非法；后随一个合法帧也必须随连接一同丢弃。
        writer.write(struct.pack(">HHHB", 900, 0, 0, 1) + fc03(100, 2))
        writer.write(mbap(901, 1, fc03(FEEDBACK_FLOAT, 2)))
        await writer.drain()
        chunk = await reader.read()
        assert chunk == b""  # 对端关闭
    finally:
        writer.close()
    # 其他连接不受影响。
    dc = await harness.client(COLLECTOR_IP)
    try:
        r = await dc.write_registers(FEEDBACK_FLOAT, float_words(8.0), device_id=1)
        assert not r.isError()
    finally:
        dc.close()


async def test_half_frame_then_early_close_does_not_break_others(harness) -> None:
    reader, writer = await raw_connect(harness.modbus_port, COMMANDER_IP)
    writer.write(mbap(950, 1, fc03(FEEDBACK_FLOAT, 2))[:5])
    await writer.drain()
    await asyncio.sleep(0.1)
    writer.close()
    dc = await harness.client(COLLECTOR_IP)
    try:
        r = await dc.write_registers(FEEDBACK_FLOAT, float_words(6.0), device_id=1)
        assert not r.isError()
    finally:
        dc.close()


async def test_zero_quantity_writes_rejected_connection_alive(harness) -> None:
    """FC15/FC16 数量 0 的畸形请求：拒绝但不得崩溃，连接可继续服务。"""
    reader, writer = await raw_connect(harness.modbus_port, COLLECTOR_IP)
    try:
        fc15_zero = struct.pack(">BHHB", 0x0F, 0, 0, 0)
        writer.write(mbap(1000, 1, fc15_zero))
        tid, unit, body = await read_response(reader)
        assert (tid, unit) == (1000, 1)
        assert body[0] & 0x80  # 异常响应，不允许虚假成功

        fc16_zero = struct.pack(">BHHB", 0x10, 100, 0, 0)
        writer.write(mbap(1001, 1, fc16_zero))
        tid, unit, body = await read_response(reader)
        assert (tid, unit) == (1001, 1)
        assert body[0] & 0x80

        # 后续合法请求照常工作。
        writer.write(mbap(1002, 1, fc16(FEEDBACK_FLOAT, float_words(3.0))))
        tid, unit, body = await read_response(reader)
        assert (tid, unit, body[0]) == (1002, 1, 0x10)
    finally:
        writer.close()


async def test_oversized_mbap_length_closes_connection(harness) -> None:
    """MBAP length=255 超出 Modbus TCP PDU 上限：按畸形帧关闭连接。"""
    reader, writer = await raw_connect(harness.modbus_port, COMMANDER_IP)
    try:
        writer.write(struct.pack(">HHHB", 1100, 0, 255, 1) + b"\x03" + b"\x00" * 254)
        await writer.drain()
        chunk = await reader.read()
        assert chunk == b""
    finally:
        writer.close()


async def test_disconnect_during_unknown_outcome_keeps_lock(harness) -> None:
    """UNKNOWN 在途期间客户端断连：闭锁不得解除，重连重发不得再调用 Commander。"""
    commander: FakeCommanderService = harness.commander
    commander.set_delay("phys1", "spu", 0.5)
    commander.set_outcome("phys1", "spu", pb.COMMAND_OUTCOME_UNKNOWN)
    reader, writer = await raw_connect(harness.modbus_port, COMMANDER_IP)
    writer.write(mbap(990, 1, fc06(210, 4242)))
    await writer.drain()
    await asyncio.sleep(0.1)
    writer.close()
    await asyncio.sleep(0.8)
    assert len(commander.calls) == 1

    dc = await harness.client(COMMANDER_IP)
    try:
        retry = await dc.write_register(210, 4242, device_id=1)
        assert retry.isError()
        # 同设备其他控制点仍可控（闭锁粒度正确）。
        other = await dc.write_coil(32, True, device_id=1)
        assert not other.isError(), other
    finally:
        dc.close()
    assert len(commander.calls) == 2  # 仅新增的是 cmd_coil，锁定点零重发
    assert all(c.point_id != "spu" or i == 0 for i, c in enumerate(commander.calls))
