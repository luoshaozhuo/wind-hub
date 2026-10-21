"""Modbus 请求来源上下文：仅在单个请求的协程中传播客户端 IP。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from ipaddress import ip_address

from pymodbus.server import ModbusTcpServer
from pymodbus.server.requesthandler import ServerRequestHandler

_request_ip: ContextVar[str | None] = ContextVar("gateway_request_ip", default=None)


def request_ip() -> str | None:
    """只允许协议请求执行期间访问，不把来源身份保存在全局对象上。"""
    return _request_ip.get()


class _GatewayRequestHandler(ServerRequestHandler):
    """TCP 分帧后逐请求串行分发，避免库丢弃同段后续事务。"""

    def __init__(self, owner, trace_packet, trace_pdu, trace_connect) -> None:
        super().__init__(owner, trace_packet, trace_pdu, trace_connect)
        self._tcp_pending = bytearray()
        self._request_queue: asyncio.Queue[tuple[object, tuple | None]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    def datagram_received(self, data: bytes, addr: tuple | None) -> None:
        self._tcp_pending.extend(data)
        if len(self._tcp_pending) > 65536:
            self._tcp_pending.clear()
            if self.transport:
                self.transport.close()
            return
        while len(self._tcp_pending) >= 7:
            length = int.from_bytes(self._tcp_pending[4:6], "big")
            if not 2 <= length <= 254:
                self._tcp_pending.clear()
                if self.transport:
                    self.transport.close()
                return
            frame_size = 6 + length
            if len(self._tcp_pending) < frame_size:
                break
            frame = bytes(self._tcp_pending[:frame_size])
            del self._tcp_pending[:frame_size]
            super().datagram_received(frame, addr)

    def handle_later(self) -> None:
        if self.last_pdu is None:
            return
        self._request_queue.put_nowait((self.last_pdu, self.last_addr))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_queue())

    async def _run_queue(self) -> None:
        while not self._request_queue.empty():
            pdu, addr = self._request_queue.get_nowait()
            self.last_pdu = pdu
            self.last_addr = addr
            await self.handle_request()
    async def handle_request(self) -> None:
        transport = self.transport
        peer = transport.get_extra_info("peername") if transport else None
        remote = str(ip_address(peer[0])) if isinstance(peer, tuple) and peer else None
        token = _request_ip.set(remote)
        try:
            await super().handle_request()
        finally:
            _request_ip.reset(token)


class GatewayTcpServer(ModbusTcpServer):
    def callback_new_connection(self) -> ServerRequestHandler:
        return _GatewayRequestHandler(
            self, self.trace_packet, self.trace_pdu, self.trace_connect
        )
