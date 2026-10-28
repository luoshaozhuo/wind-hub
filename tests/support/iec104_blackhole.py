"""只维持 APCI 传输层存活、永不回应应用层的 TCP 假 IEC104 从站。

应答 STARTDT_CON、对每个 I 帧回 S 帧传输确认（接收序号），但从不发送
任何 ASDU 响应。用于真实 TCP 路径上的超时分类测试：传输 ACK 与应用
确认必须严格区分——S 帧绝不构成 ACT_CON / C_RD 响应证据。
"""

from __future__ import annotations

import asyncio


class StartdtBlackhole:
    """TCP 黑洞从站：STARTDT/S 帧应答，零应用层响应。"""

    _STARTDT_ACT = bytes.fromhex("680407000000")
    _STARTDT_CON = bytes.fromhex("68040b000000")

    def __init__(self, port: int) -> None:
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self._port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        buffer = bytearray()
        recv_seq = 0
        try:
            while True:
                data = await reader.read(255)
                if not data:
                    break
                buffer += data
                # APCI 帧可能跨 TCP 读到达，残余字节缓冲到下次处理。
                response, consumed, recv_seq = self._respond(bytes(buffer), recv_seq)
                del buffer[:consumed]
                if response:
                    writer.write(response)
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    @classmethod
    def _respond(cls, data: bytes, recv_seq: int) -> tuple[bytes, int, int]:
        """逐帧处理；只回 STARTDT_CON 与 S 帧传输确认。"""
        out = bytearray()
        offset = 0
        while offset + 6 <= len(data):
            if data[offset] != 0x68:
                break
            length = data[offset + 1]
            frame = data[offset : offset + 2 + length]
            if len(frame) < 2 + length:
                break
            control = frame[2:6]
            if frame == cls._STARTDT_ACT:
                out += cls._STARTDT_CON
            elif control[0] & 0x01 == 0:  # I 帧：S 帧确认，无应用响应
                recv_seq += 1
                out += bytes([0x68, 0x04, 0x01, 0x00])
                out += (recv_seq * 2).to_bytes(2, "little")
            offset += 2 + length
        return bytes(out), offset, recv_seq

    async def stop(self) -> None:
        for writer in list(self._writers):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
