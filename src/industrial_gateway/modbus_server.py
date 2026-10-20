"""最小 Modbus TCP 服务端：按完整映射点原子处理读写。

仅实现 FC01/02/03/04/05/06/15/16。禁用广播、诊断与部分控制值写入。
单 TCP 连接请求串行处理，不将控制结果伪装成现场执行成功。
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Iterable

from .domain import PointWrite
from .modbus_codec import RegisterMap, RegisterMapping, decode_registers, encode_registers
from .point_store import PointStore

logger = logging.getLogger(__name__)

_READ_AREAS = {1: "coil", 2: "discrete_input", 3: "holding", 4: "input"}
_WRITE_AREAS = {5: "coil", 6: "holding", 15: "coil", 16: "holding"}
_LIMITS = {1: 2000, 2: 2000, 3: 125, 4: 125, 15: 1968, 16: 123}


class ModbusException(Exception):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"Modbus exception {code}")


class ModbusTcpGateway:
    """一个监听端点对应一个逻辑设备；按 TCP 连接隔离通信故障。"""

    def __init__(
        self,
        store: PointStore,
        mappings: Iterable[RegisterMapping],
        *,
        unit_id: int = 1,
        write_source: str = "modbus",
        control_client_ips: frozenset[str] = frozenset(),
    ) -> None:
        if not 1 <= unit_id <= 247:
            raise ValueError("unit_id must be 1..247")
        if not write_source:
            raise ValueError("write_source must not be empty")
        self._store = store
        self._mappings = list(mappings)
        self._register_map = RegisterMap(self._mappings)
        self._unit_id = unit_id
        self._write_source = write_source
        self._control_client_ips = control_client_ips
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._available = True

    async def start(self, host: str, port: int) -> None:
        if self._server is not None:
            raise RuntimeError("server already started")
        self._server = await asyncio.start_server(self._client, host, port)

    async def set_available(self, available: bool) -> None:
        """关键反馈不可用时停止该虚拟设备服务并断开现有会话。"""
        self._available = available
        if not available:
            clients = tuple(self._clients)
            for writer in clients:
                writer.close()
            if clients:
                await asyncio.gather(
                    *(writer.wait_closed() for writer in clients), return_exceptions=True
                )

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        clients = tuple(self._clients)
        for writer in clients:
            writer.close()
        if clients:
            await asyncio.gather(*(writer.wait_closed() for writer in clients), return_exceptions=True)

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._clients.add(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                transaction, protocol, length, unit = struct.unpack(">HHHB", header)
                if protocol != 0 or not 2 <= length <= 254:
                    break
                payload = await reader.readexactly(length - 1)
                if unit != self._unit_id:
                    continue
                if not self._available:
                    break
                function = payload[0]
                try:
                    peer = writer.get_extra_info("peername")
                    if function in _WRITE_AREAS and (
                        peer is None or peer[0] not in self._control_client_ips
                    ):
                        raise ModbusException(2)
                    response = await self._execute(function, payload[1:])
                except ModbusException as exc:
                    response = bytes((function | 0x80, exc.code))
                except Exception:
                    logger.exception("Modbus gateway request failed")
                    response = bytes((function | 0x80, 4))
                frame = struct.pack(">HHHB", transaction, 0, len(response) + 1, unit)
                writer.write(frame + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _execute(self, function: int, data: bytes) -> bytes:
        if function in _READ_AREAS:
            if len(data) != 4:
                raise ModbusException(3)
            start, count = struct.unpack(">HH", data)
            if not 1 <= count <= _LIMITS[function]:
                raise ModbusException(3)
            area = _READ_AREAS[function]
            values = self._read_area(area, start, count)
            if function in (1, 2):
                packed = bytearray((count + 7) // 8)
                for index, value in enumerate(values):
                    packed[index // 8] |= (value & 1) << (index % 8)
                return bytes((function, len(packed))) + packed
            return bytes((function, count * 2)) + struct.pack(f">{count}H", *values)
        if function in _WRITE_AREAS:
            return await self._write(function, data)
        raise ModbusException(1)

    def _read_area(self, area: str, start: int, count: int) -> list[int]:
        if start + count > 65536:
            raise ModbusException(2)
        words: dict[int, int] = {}
        for mapping in self._mappings:
            if mapping.area != area:
                continue
            begin, end = mapping.address, mapping.address + mapping.count
            if end <= start or begin >= start + count:
                continue
            value = self._store.read(mapping.device_id, mapping.point_id).value
            if value is None:
                raise ModbusException(4)
            try:
                encoded = encode_registers(mapping, value)
            except (ValueError, TypeError):
                raise ModbusException(4) from None
            words.update({begin + i: word for i, word in enumerate(encoded)})
        try:
            return [words[i] for i in range(start, start + count)]
        except KeyError:
            raise ModbusException(2) from None

    async def _write(self, function: int, data: bytes) -> bytes:
        if function in (5, 6):
            if len(data) != 4:
                raise ModbusException(3)
            start, value = struct.unpack(">HH", data)
            if function == 5 and value not in (0x0000, 0xFF00):
                raise ModbusException(3)
            words = [int(value == 0xFF00)] if function == 5 else [value]
            echo = data
        else:
            if len(data) < 5:
                raise ModbusException(3)
            start, count, byte_count = struct.unpack(">HHB", data[:5])
            if not 1 <= count <= _LIMITS[function]:
                raise ModbusException(3)
            expected = (count + 7) // 8 if function == 15 else count * 2
            if byte_count != expected or len(data) != expected + 5:
                raise ModbusException(3)
            if function == 15:
                words = [
                    (data[5 + i // 8] >> (i % 8)) & 1 for i in range(count)
                ]
            else:
                words = list(struct.unpack(f">{count}H", data[5:]))
            echo = struct.pack(">HH", start, count)
        area = _WRITE_AREAS[function]
        try:
            mapping = self._register_map.resolve_write(area, start, len(words))
        except (PermissionError, ValueError):
            raise ModbusException(2) from None
        try:
            value = decode_registers(mapping, words)
        except (ValueError, TypeError):
            raise ModbusException(3) from None
        try:
            await self._store.write(
                PointWrite(mapping.device_id, mapping.point_id, value, self._write_source)
            )
        except (PermissionError, KeyError):
            raise ModbusException(2) from None
        except (RuntimeError, ValueError, TypeError):
            raise ModbusException(4) from None
        return bytes((function,)) + echo
