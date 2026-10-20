"""Modbus TCP 端到端协议帧验证。"""

import asyncio
import struct

import pytest

from industrial_gateway.domain import PointDefinition, PointRole
from industrial_gateway.modbus_codec import RegisterMapping
from industrial_gateway.modbus_server import ModbusException, ModbusTcpGateway
from industrial_gateway.point_store import PointStore


async def _exchange(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    function: int,
    arguments: bytes,
) -> bytes:
    payload = bytes((function,)) + arguments
    writer.write(struct.pack(">HHHB", 7, 0, len(payload) + 1, 1) + payload)
    await writer.drain()
    header = await reader.readexactly(7)
    transaction, protocol, length, unit = struct.unpack(">HHHB", header)
    assert (transaction, protocol, unit) == (7, 0, 1)
    return await reader.readexactly(length - 1)


async def test_two_clients_share_registers_and_partial_write_fails() -> None:
    store = PointStore([PointDefinition("t1", "parameter", PointRole.PARAMETER, writable=True)])
    server = ModbusTcpGateway(
        store, [RegisterMapping("t1", "parameter", "holding", 100, "float32")]
    )
    await server.start("127.0.0.1", 0)
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
    second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        assert await _exchange(
            first_reader, first_writer, 6, struct.pack(">HH", 100, 0)
        ) == bytes((0x86, 2))
        assert await _exchange(
            first_reader, first_writer, 16,
            struct.pack(">HHBHH", 100, 2, 4, 0x42C8, 0),
        ) == bytes((16,)) + struct.pack(">HH", 100, 2)
        result = await _exchange(
            second_reader, second_writer, 3, struct.pack(">HH", 100, 2)
        )
        assert result == bytes((3, 4)) + struct.pack(">HH", 0x42C8, 0)
    finally:
        first_writer.close()
        second_writer.close()
        await first_writer.wait_closed()
        await second_writer.wait_closed()
        await server.stop()


async def test_command_rejected_without_handler() -> None:
    store = PointStore([PointDefinition("t1", "command", PointRole.COMMAND, writable=True)])
    server = ModbusTcpGateway(
        store, [RegisterMapping("t1", "command", "holding", 3, "uint16")]
    )
    with pytest.raises(ModbusException) as exc:\n        await server._execute(6, struct.pack(">HH", 3, 5))\n    assert exc.value.code == 4
    assert store.read("t1", "command").version == 0


@pytest.mark.parametrize("function", [1, 2, 3, 4])
async def test_unmapped_read_returns_illegal_address(function: int) -> None:
    store = PointStore([])
    server = ModbusTcpGateway(store, [])
    assert await server._execute(function, struct.pack(">HH", 0, 1)) == bytes(
        (function | 0x80, 2)
    )
