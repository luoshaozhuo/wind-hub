"""Modbus 请求来源上下文：仅在单个请求的协程中传播客户端 IP。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from ipaddress import ip_address

from pymodbus.constants import ExcCodes
from pymodbus.exceptions import NoSuchIdException
from pymodbus.pdu import ExceptionResponse, ModbusPDU

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
        self._request_queue: asyncio.Queue[tuple[ModbusPDU, tuple | None]] = asyncio.Queue(maxsize=64)
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

    def callback_data(self, data: bytes, addr: tuple | None = None) -> int:
        cut = super().callback_data(data, addr)
        if self.last_pdu is not None:
            try:
                self._request_queue.put_nowait((self.last_pdu, self.last_addr))
            except asyncio.QueueFull:
                if self.transport:
                    self.transport.close()
                return cut
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._run_queue())
        return cut

    def handle_later(self) -> None:
        # 已在 callback_data 中复制请求，取消父类的延后共享字段读取。
        return

    async def _run_queue(self) -> None:
        while not self._request_queue.empty():
            pdu, addr = self._request_queue.get_nowait()
            await self._handle_one(pdu, addr)

    async def _handle_one(self, pdu: ModbusPDU, addr: tuple | None) -> None:
        # 来源身份绑定本次请求，PDU 与事务 ID 都是局部变量。
        transport = self.transport
        peer = transport.get_extra_info("peername") if transport else None
        remote = str(ip_address(peer[0])) if isinstance(peer, tuple) and peer else None
        token = _request_ip.set(remote)
        try:
            try:
                response = await pdu.datastore_update(self.server.context, pdu.dev_id)
            except (NoSuchIdException, KeyError):
                response = ExceptionResponse(pdu.function_code, ExcCodes.GATEWAY_NO_RESPONSE)
            except Exception:
                response = ExceptionResponse(pdu.function_code, ExcCodes.DEVICE_FAILURE)
            response.transaction_id = pdu.transaction_id
            response.dev_id = pdu.dev_id
            self.server_send(response, addr)
        finally:
            _request_ip.reset(token)

    def callback_disconnected(self, exc: Exception | None) -> None:
        if self._worker is not None and self._worker.done():
            self._worker = None
        super().callback_disconnected(exc)



class GatewayTcpServer(ModbusTcpServer):
    def callback_new_connection(self) -> ServerRequestHandler:
        return _GatewayRequestHandler(
            self, self.trace_packet, self.trace_pdu, self.trace_connect
        )
