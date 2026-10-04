"""Modbus TCP 写响应丢弃代理——模拟「PLC 已执行写命令但响应包丢失」。

客户端（Commander/Collector）连接代理端口，代理转发到真实从站。
``drop_write_responses`` 置位时，从站→客户端方向上功能码属于写类
（FC5/6/15/16，含对应异常响应）的 MBAP 帧被丢弃：从站照常执行写入，
客户端只能观察到超时——这是「命令执行结果未知」的唯一诚实注入方式
（停服务制造的是连接断开，不是响应丢失）。

读响应与请求方向不受影响，因此同一链路上的采集流量正常。
"""

from __future__ import annotations

import asyncio
import contextlib

_WRITE_FUNCTION_CODES = frozenset({5, 6, 15, 16})

#: MBAP 头长（txn 2 + proto 2 + length 2 + unit 1）；PDU 功能码在偏移 7。
_MBAP_HEADER = 7


class WriteResponseDropProxy:
    """异步 TCP 代理：监听本地端口，按帧过滤后转发到目标从站。"""

    def __init__(self, target_host: str, target_port: int, listen_port: int) -> None:
        self._target = (target_host, target_port)
        self._listen_port = listen_port
        #: 测试开关：置 True 后丢弃写响应帧。
        self.drop_write_responses = False
        #: 累计被丢弃的写响应帧数（断言注入确实生效）。
        self.dropped_responses = 0
        self._server: asyncio.AbstractServer | None = None
        self._peers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        return self._listen_port

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client, "127.0.0.1", self._listen_port
        )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in list(self._peers):
            with contextlib.suppress(Exception):
                writer.close()
        self._peers.clear()
        # start_server 为每条连接派生的 handler 是独立 asyncio task，
        # 关闭对端 writer 后等它们实际退出——不留下 pending task。
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)

    def _on_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.create_task(self._handle_client(client_reader, client_writer))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        except OSError:
            client_writer.close()
            return
        self._peers.update((client_writer, upstream_writer))
        try:
            await asyncio.gather(
                self._pipe_raw(client_reader, upstream_writer),
                self._pipe_responses(upstream_reader, client_writer),
            )
        finally:
            self._peers.discard(client_writer)
            self._peers.discard(upstream_writer)

    async def _pipe_raw(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """请求方向：不过滤，原样转发。"""
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()

    async def _pipe_responses(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """响应方向：按 MBAP 帧边界解析，按开关丢弃写响应。"""
        buffer = bytearray()
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            while chunk := await reader.read(65536):
                buffer.extend(chunk)
                offset = 0
                while True:
                    frame = self._next_frame(buffer, offset)
                    if frame is None:
                        break
                    end, function_code = frame
                    raw = bytes(buffer[offset:end])
                    offset = end
                    if (
                        self.drop_write_responses
                        and function_code in _WRITE_FUNCTION_CODES
                    ):
                        self.dropped_responses += 1
                        continue
                    writer.write(raw)
                del buffer[:offset]
                await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()

    @staticmethod
    def _next_frame(buffer: bytearray, offset: int) -> tuple[int, int] | None:
        """从 ``offset`` 起解析一条完整 MBAP 帧。

        Returns:
            ``(帧尾偏移, 功能码)``；缓冲不足一帧时返回 None（等更多数据）。
        """
        if len(buffer) - offset < _MBAP_HEADER + 1:
            return None
        pdu_len = int.from_bytes(buffer[offset + 4 : offset + 6], "big")
        end = offset + 6 + pdu_len
        if len(buffer) < end:
            return None
        # 功能码低 7 位（异常响应 = 请求功能码 | 0x80，同样属于写响应）。
        function_code = buffer[offset + _MBAP_HEADER] & 0x7F
        return end, function_code
