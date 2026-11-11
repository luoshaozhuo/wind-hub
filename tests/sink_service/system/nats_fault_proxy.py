"""NATS 线协议故障代理：在真实 TCP 路径上选择性丢弃客户端 PUB 帧。

S3-03（ACK 丢失 -> at-least-once 重投）需要在不修改 NATS 服务端、不
Mock 任何组件的前提下，让 JetStream 客户端的 ACK 发布帧在线路上消失。
NATS 客户端协议是行分隔文本头 + 定长载荷：

- ``PUB <subject> <reply> <nbytes>\\r\\n<payload>\\r\\n``
- ``HPUB <subject> <reply> <hdr-bytes> <nbytes>\\r\\n<payload>\\r\\n``

代理在客户端 -> 服务端方向逐帧解析头行，按可配置谓词决定整帧转发或
静默丢弃（载荷仍从 socket 读完以保持流同步）；服务端 -> 客户端方向
完全字节透传（MSG/PING/PONG/+OK/-ERR 不做任何改写）。ACK 是发往
``$JS.ACK.<stream>.<consumer>...`` 主题的 PUB，因此 drop 谓词按主题
前缀匹配即可精确模拟 ACK 丢失，不影响普通消息发布。

仅用于系统测试，明文 NATS（``nats://``）；生产链路若启用 TLS 则需在
TLS 边界内侧注入，这是本代理明确不覆盖的环境条件。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

LOGGER = logging.getLogger(__name__)

#: JetStream 客户端 ACK 发布主题前缀（NATS 内置保留命名空间）。
JS_ACK_PREFIX = "$JS.ACK."

DropPredicate = Callable[[str], bool]


def drop_jetstream_ack(subject: str) -> bool:
    """丢弃所有 JetStream ACK 帧的默认谓词。"""
    return subject.startswith(JS_ACK_PREFIX)


class NatsFaultProxy:
    """单上游、多客户端连接的选择性丢帧 TCP 代理。"""

    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        *,
        listen_host: str = "127.0.0.1",
        drop: DropPredicate | None = None,
    ) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._listen_host = listen_host
        self._drop: DropPredicate = drop or (lambda _subject: False)
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self.dropped_frames = 0
        self.forwarded_pub_frames = 0
        self.connections = 0

    @property
    def port(self) -> int:
        if not self._port:
            raise RuntimeError("proxy is not started")
        return self._port

    @property
    def address(self) -> str:
        return f"nats://{self._listen_host}:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self._listen_host, 0)
        sock = self._server.sockets[0]
        self._port = sock.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> NatsFaultProxy:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self._upstream[0], self._upstream[1]
            )
        except OSError:
            writer.close()
            with contextlib.suppress(ConnectionError, BrokenPipeError):
                await writer.wait_closed()
            return
        try:
            await asyncio.gather(
                self._pump_client_to_server(reader, upstream_writer),
                self._pump_raw(upstream_reader, writer),
            )
        except (ConnectionError, asyncio.IncompleteReadError, BrokenPipeError):
            pass
        finally:
            for socket_writer in (writer, upstream_writer):
                socket_writer.close()
                with contextlib.suppress(ConnectionError, BrokenPipeError):
                    await socket_writer.wait_closed()

    async def _pump_raw(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """服务端 -> 客户端：字节透传，不解析不改写。"""
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                writer.close()
                return
            writer.write(chunk)
            await writer.drain()

    async def _pump_client_to_server(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """客户端 -> 服务端：识别 PUB/HPUB 帧并按谓词丢弃，其余透传。"""
        while True:
            try:
                header = await reader.readuntil(b"\r\n")
            except asyncio.IncompleteReadError:
                writer.close()
                return
            if not header:
                writer.close()
                return
            tokens = header.rstrip(b"\r\n").split(b" ")
            command = tokens[0] if tokens else b""
            if command in (b"PUB", b"HPUB"):
                # PUB  <subject> <reply> <nbytes>
                # HPUB <subject> <reply> <hdr-bytes> <nbytes>
                subject = tokens[1].decode("ascii", errors="strict")
                payload_size = int(tokens[-1])
                payload = await reader.readexactly(payload_size + 2)
                if self._drop(subject):
                    self.dropped_frames += 1
                    LOGGER.info("fault proxy dropped PUB frame subject=%s", subject)
                    continue
                self.forwarded_pub_frames += 1
                writer.write(header + payload)
                await writer.drain()
            else:
                # CONNECT/PING/SUB/UNSUB 等无载荷或自带定长语义的帧：
                # 这些命令均为单行，直接透传整行。
                writer.write(header)
                await writer.drain()


async def wait_until(
    predicate: Callable[[], Awaitable[bool]], *, timeout: float, interval: float = 0.5
) -> None:
    """测试轮询助手（代理专用，避免与系统测试夹具的等待器耦合）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError("fault proxy condition not reached before deadline")
