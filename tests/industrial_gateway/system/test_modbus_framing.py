"""裸 TCP socket 分帧/流水线/连接边界系统测试（真实网络字节，无 Mock）。

pymodbus 客户端无法控制 TCP 段切分与流水线，这里手工构造 MBAP/PDU：
- 同段多请求、半包重组、半包+完整帧拼接、TID 乱序对应；
- 多连接并发，每请求恰好一个响应；
- 每连接 64 深队列溢出关连接；
- 畸形 MBAP length（0/255）关连接且不殃及其他连接；
- 半包早断连隔离；零数量 FC15/16 不虚假成功且连接存活；错误 Unit → 0x0B 后可恢复。
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest

from tests.support.gateway_v1 import (
    COMMAND_UINT,
    COMMAND_UINT2,
    FEEDBACK_PAIR,
    FEEDBACK_UINT,
    POINT_TABLE,
    start_gateway_v1,
    two_server_specs,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.modbus,
    pytest.mark.fast,
]

ADDR: dict[str, int] = {name: spec["address"]["address"] for name, spec in POINT_TABLE.items()}


def mbap(tid: int, unit: int, pdu: bytes) -> bytes:
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def fc03(start: int, count: int) -> bytes:
    return struct.pack(">BHH", 0x03, start, count)


def fc16(start: int, words: list[int]) -> bytes:
    pdu = struct.pack(">BHHB", 0x10, start, len(words), len(words) * 2)
    return pdu + b"".join(struct.pack(">H", w) for w in words)


def fc15(start: int, bits: list[bool]) -> bytes:
    byte_count = (len(bits) + 7) // 8
    packed = 0
    for index, bit in enumerate(bits):
        packed |= int(bit) << index
    pdu = struct.pack(">BHHB", 0x0F, start, len(bits), byte_count)
    return pdu + packed.to_bytes(byte_count, "big")


async def _connect(port: int):
    return await asyncio.open_connection("127.0.0.1", port)


async def _read_response(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    header = await reader.readexactly(7)
    tid, _proto, length, unit = struct.unpack(">HHHB", header)
    body = await reader.readexactly(length - 1)
    return tid, unit, body


@pytest.fixture
async def harness(tmp_path: Path):
    gateway = await start_gateway_v1(tmp_path / "root", two_server_specs())
    try:
        yield gateway
    finally:
        await gateway.stop()


async def test_two_complete_requests_in_one_segment(harness) -> None:
    port = harness.modbus_port("gw1")
    reader, writer = await _connect(port)
    try:
        # c387e0b81 后仅受控（command_mappings 所有）点接受 Modbus 写，
        # 分帧测试改用受控保持寄存器，成功响应仍应为 FC16（0x10）而非异常帧。
        blob = mbap(10, 1, fc16(ADDR[COMMAND_UINT2], [1]))
        blob += mbap(20, 1, fc16(ADDR[COMMAND_UINT], [2]))
        writer.write(blob)
        r1 = await _read_response(reader)
        r2 = await _read_response(reader)
        assert (r1[0], r1[1], r1[2][0]) == (10, 1, 0x10)
        assert (r2[0], r2[1], r2[2][0]) == (20, 1, 0x10)
    finally:
        writer.close()


async def test_single_request_split_across_segments(harness) -> None:
    port = harness.modbus_port("gw1")
    reader, writer = await _connect(port)
    try:
        frame = mbap(30, 1, fc03(ADDR[FEEDBACK_UINT], 1))
        writer.write(frame[:4])
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.write(frame[4:])
        tid, unit, body = await _read_response(reader)
        assert (tid, unit, body[0]) == (30, 1, 0x03)
        assert body[1] == 2  # 1 个寄存器 = 2 字节
    finally:
        writer.close()


async def test_half_packet_then_full_frame_in_same_segment(harness) -> None:
    port = harness.modbus_port("gw1")
    reader, writer = await _connect(port)
    try:
        first = mbap(40, 1, fc03(ADDR[FEEDBACK_PAIR], 1))
        second = mbap(41, 1, fc03(ADDR[FEEDBACK_UINT], 1))
        writer.write(first[:6])
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.write(first[6:] + second)
        r1 = await _read_response(reader)
        r2 = await _read_response(reader)
        assert r1[0] == 40 and r2[0] == 41  # TID 不串
    finally:
        writer.close()


async def test_interleaved_tids_one_segment_match_by_tid(harness) -> None:
    port = harness.modbus_port("gw1")
    reader, writer = await _connect(port)
    try:
        blob = b"".join(
            mbap(tid, 1, fc03(addr, 1))
            for tid, addr in (
                (505, ADDR[FEEDBACK_PAIR]),
                (503, ADDR[FEEDBACK_UINT]),
                (501, ADDR[FEEDBACK_PAIR]),
            )
        )
        writer.write(blob)
        tids = [(await _read_response(reader))[0] for _ in range(3)]
        assert tids == [505, 503, 501]  # 严格逐请求串行，响应按入站顺序
    finally:
        writer.close()


async def test_multi_connection_concurrency_one_response_each(harness) -> None:
    port = harness.modbus_port("gw1")

    async def one(conn_id: int) -> None:
        reader, writer = await _connect(port)
        try:
            for sub in range(8):
                tid = conn_id * 100 + sub
                writer.write(mbap(tid, 1, fc03(ADDR[FEEDBACK_UINT], 1)))
            await writer.drain()
            seen = [(await _read_response(reader))[0] for _ in range(8)]
            assert seen == [conn_id * 100 + sub for sub in range(8)]
        finally:
            writer.close()

    await asyncio.gather(*(one(conn_id) for conn_id in range(4)))


async def test_queue_overflow_closes_connection_and_bounds_execution(
    harness,
) -> None:
    """第 65 个排队请求触发关连接；至多 64 个请求得到响应。"""
    port = harness.modbus_port("gw1")
    reader, writer = await _connect(port)
    try:
        # 全部帧同段到达：回调同步入队 64 个后第 65 个触发 QueueFull→关连接。
        writer.write(
            b"".join(mbap(1000 + index, 1, fc03(ADDR[FEEDBACK_PAIR], 1)) for index in range(70))
        )
        await writer.drain()
        responses = 0
        try:
            while True:
                await asyncio.wait_for(_read_response(reader), timeout=2.0)
                responses += 1
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        assert responses <= 64
    finally:
        writer.close()


async def test_malformed_zero_length_closes_and_isolates_other_connections(
    harness,
) -> None:
    port = harness.modbus_port("gw1")
    bad_reader, bad_writer = await _connect(port)
    good_reader, good_writer = await _connect(port)
    try:
        bad_writer.write(struct.pack(">HHHB", 900, 0, 0, 1) + b"\x03")
        await bad_writer.drain()
        with pytest.raises(asyncio.IncompleteReadError):
            await asyncio.wait_for(bad_reader.readexactly(7), timeout=2.0)
        # 同连接后续合法帧一并丢弃。
        bad_writer.write(mbap(901, 1, fc03(ADDR[FEEDBACK_PAIR], 1)))
        # 其他连接不受影响。
        good_writer.write(mbap(902, 1, fc03(ADDR[FEEDBACK_UINT], 1)))
        tid, _unit, body = await _read_response(good_reader)
        assert tid == 902 and body[0] == 0x03
    finally:
        bad_writer.close()
        good_writer.close()


async def test_oversized_mbap_length_closes_connection(harness) -> None:
    port = harness.modbus_port("gw1")
    reader, writer = await _connect(port)
    try:
        # length=255 超 PDU 上限：只发 7 字节头也必须关连接。
        writer.write(struct.pack(">HHHB", 910, 0, 255, 1))
        await writer.drain()
        with pytest.raises(asyncio.IncompleteReadError):
            await asyncio.wait_for(reader.readexactly(1), timeout=2.0)
    finally:
        writer.close()


async def test_half_frame_early_close_does_not_break_others(harness) -> None:
    port = harness.modbus_port("gw1")
    dying_reader, dying_writer = await _connect(port)
    good_reader, good_writer = await _connect(port)
    try:
        dying_writer.write(mbap(920, 1, fc03(ADDR[FEEDBACK_PAIR], 1))[:5])
        await dying_writer.drain()
        await asyncio.sleep(0.1)
        dying_writer.close()
        with pytest.raises(asyncio.IncompleteReadError):
            await dying_reader.readexactly(7)
        good_writer.write(mbap(921, 1, fc03(ADDR[FEEDBACK_UINT], 1)))
        tid, _unit, body = await _read_response(good_reader)
        assert tid == 921 and body[0] == 0x03
    finally:
        good_writer.close()


async def test_zero_quantity_writes_rejected_connection_alive(harness) -> None:
    port = harness.modbus_port("gw1")
    reader, writer = await _connect(port)
    try:
        # FC16 数量 0（byte count 0）：不虚假成功。
        writer.write(mbap(930, 1, struct.pack(">BHHB", 0x10, ADDR[FEEDBACK_PAIR], 0, 0)))
        tid, _unit, body = await _read_response(reader)
        assert tid == 930
        assert body[0] & 0x80  # 异常响应
        # FC15 数量 0。
        writer.write(mbap(931, 1, struct.pack(">BHH", 0x0F, 0, 0)))
        tid, _unit, body = await _read_response(reader)
        assert tid == 931
        assert body[0] & 0x80
        # 连接继续可用。
        writer.write(mbap(932, 1, fc03(ADDR[FEEDBACK_UINT], 1)))
        tid, _unit, body = await _read_response(reader)
        assert (tid, body[0]) == (932, 0x03)
    finally:
        writer.close()


async def test_wrong_unit_returns_0b_then_connection_recovers(harness) -> None:
    port = harness.modbus_port("gw1")
    reader, writer = await _connect(port)
    try:
        writer.write(mbap(940, 99, fc03(ADDR[FEEDBACK_PAIR], 1)))
        _tid, _unit, body = await _read_response(reader)
        assert body[0] & 0x80
        assert body[1] == 0x0B  # GATEWAY_NO_RESPONSE
        writer.write(mbap(941, 1, fc03(ADDR[FEEDBACK_UINT], 1)))
        _tid, _unit, body = await _read_response(reader)
        assert body[0] == 0x03
    finally:
        writer.close()
