"""测试用 TCP 故障代理：在真实 TCP 端点之间注入网络故障。

ADS 客户端连接代理监听端口，代理把字节透明转发到真实服务端。支持：

- ``forward``：正常双向透传，可注入固定延迟；
- ``blackhole``：接受连接但不转发、不响应（模拟黑洞/防火墙丢包）；
- ``drop()``：立即 RST/FIN 全部已建立连接（模拟链路中断/SIGKILL 后 socket 关闭）；
- 帧计数与 ``drop_after_frames``：转发指定数量 AMS/TCP 帧后切断（模拟
  注册/注销途中的断链）。AMS/TCP 帧头为 2 字节保留字（0x0000）+
  4 字节小端无符号负载长度。

代理只操作 TCP 字节流，不解析 AMS 语义，因此绝不与「ADS Runtime STOP」
混淆：STOP 是 TCP 保持连接、设备状态变化，可由服务端 set_running 控制。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProxyStats:
    frames_forwarded: int
    open_connections: int


class TCPFaultProxy:
    def __init__(
        self,
        *,
        upstream_host: str,
        upstream_port: int,
        listen_host: str = "127.0.0.1",
        delay: float = 0.0,
        blackhole: bool = False,
        drop_after_frames: int | None = None,
        listen_port: int = 48898,
    ) -> None:
        # pyads Linux 本地路由按 AMS Net ID 解析主机后，TCP 目的端口固定
        # 48898（851/801 是 AMS 逻辑端口），因此代理必须监听 48898。
        self._upstream = (upstream_host, upstream_port)
        self._listen_host = listen_host
        self._listen_port_requested = listen_port
        self._delay = delay
        self._blackhole = blackhole
        self._drop_after_frames = drop_after_frames
        self._server: asyncio.AbstractServer | None = None
        self._channels: set[_Channel] = set()
        self._frames = 0
        self._listen_port: int | None = None

    @property
    def listen_port(self) -> int:
        if self._listen_port is None:
            raise RuntimeError("proxy is not started")
        return self._listen_port

    @property
    def host(self) -> str:
        return self._listen_host

    def stats(self) -> ProxyStats:
        return ProxyStats(self._frames, len(self._channels))

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._accept,
            self._listen_host,
            self._listen_port_requested,
        )
        socket = self._server.sockets[0]
        self._listen_port = socket.getsockname()[1]

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        await self.drop()

    async def drop(self) -> None:
        """切断全部活动连接（客户端与上行两侧）。"""
        for channel in tuple(self._channels):
            channel.close()
        await asyncio.sleep(0)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._blackhole:
            # 保持 socket 打开但永不读取/响应；不加入通道表也不向上游连接。
            await writer.wait_closed()
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(*self._upstream)
        except OSError:
            writer.close()
            return
        channel = _Channel(reader, writer, up_reader, up_writer)
        self._channels.add(channel)
        try:
            await asyncio.gather(
                self._pump(channel, client_to_up=True),
                self._pump(channel, client_to_up=False),
            )
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            channel.close()
            self._channels.discard(channel)

    async def _pump(self, channel: _Channel, *, client_to_up: bool) -> None:
        src = channel.client_reader if client_to_up else channel.up_reader
        dst = channel.up_writer if client_to_up else channel.client_writer
        while True:
            prefix = await src.readexactly(2)
            length_header = await src.readexactly(4)
            length = int.from_bytes(length_header, "little")
            payload = await src.readexactly(length)
            if client_to_up:
                self._frames += 1
                if self._drop_after_frames is not None and self._frames >= self._drop_after_frames:
                    channel.close()
                    return
            if self._delay:
                await asyncio.sleep(self._delay)
            dst.write(prefix + length_header + payload)
            await dst.drain()


class _Channel:
    def __init__(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
    ) -> None:
        self.client_reader = client_reader
        self.client_writer = client_writer
        self.up_reader = up_reader
        self.up_writer = up_writer

    def close(self) -> None:
        for writer in (self.client_writer, self.up_writer):
            writer.close()
