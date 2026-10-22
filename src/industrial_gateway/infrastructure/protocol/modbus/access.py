"""Modbus 请求来源上下文：仅在单个请求的协程中传播客户端 IP。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from ipaddress import ip_address
from typing import Any, cast

from pymodbus.constants import ExcCodes
from pymodbus.exceptions import NoSuchIdException
from pymodbus.pdu import ExceptionResponse, ModbusPDU
from pymodbus.server import ModbusTcpServer
from pymodbus.server.requesthandler import ServerRequestHandler

_request_ip: ContextVar[str | None] = ContextVar("gateway_request_ip", default=None)
_request_bit_span: ContextVar[tuple[int, int] | None] = ContextVar(
    "gateway_request_bit_span", default=None
)


def request_ip() -> str | None:
    """只允许协议请求执行期间访问，不把来源身份保存在全局对象上。"""
    return _request_ip.get()


def request_bit_span() -> tuple[int, int] | None:
    """返回请求的真实 bit 区间，防止字对齐扩大失效检查范围。"""
    return _request_bit_span.get()


class _GatewayRequestHandler(ServerRequestHandler):
    """TCP 分帧后逐请求串行分发，避免库丢弃同段后续事务。"""

    def __init__(
        self,
        owner: ModbusTcpServer,
        trace_packet: Callable[[bool, bytes], bytes] | None,
        trace_pdu: Callable[[bool, ModbusPDU], ModbusPDU] | None,
        trace_connect: Callable[[bool], None] | None,
    ) -> None:
        super_init = cast(Callable[..., None], super().__init__)
        super_init(owner, trace_packet, trace_pdu, trace_connect)
        self._tcp_pending = bytearray()
        self._request_queue: asyncio.Queue[tuple[ModbusPDU, tuple[Any, ...] | None]] = (
            asyncio.Queue(maxsize=64)
        )
        self._worker: asyncio.Task[None] | None = None

    def datagram_received(self, data: bytes, addr: tuple[Any, ...] | None) -> None:
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

    def callback_data(self, data: bytes, addr: tuple[Any, ...] | None = None) -> int:
        cut = super().callback_data(data, addr)
        if self.last_pdu is not None:
            try:
                self._request_queue.put_nowait((self.last_pdu, self.last_addr))
            except asyncio.QueueFull:
                # 已入队请求可能执行完成；仅拒绝继续接收新请求。
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

    async def _handle_one(self, pdu: ModbusPDU, addr: tuple[Any, ...] | None) -> None:
        # 来源身份绑定本次请求，PDU 与事务 ID 都是局部变量。
        transport = self.transport
        peer = transport.get_extra_info("peername") if transport else None
        remote = str(ip_address(peer[0])) if isinstance(peer, tuple) and peer else None
        token = _request_ip.set(remote)
        bit_span: tuple[int, int] | None = None
        bit_count: int | None = None
        if pdu.function_code in {1, 2, 5, 15}:
            bit_address = getattr(pdu, "address", None)
            if pdu.function_code == 5:
                bit_count = 1
            elif pdu.function_code == 15:
                values = getattr(pdu, "bits", None)
                bit_count = len(values) if values is not None else getattr(pdu, "count", None)
            else:
                bit_count = getattr(pdu, "count", None)
            if isinstance(bit_address, int) and isinstance(bit_count, int) and bit_count > 0:
                bit_span = (bit_address, bit_count)
        bit_token = _request_bit_span.set(bit_span)
        try:
            try:
                response = await pdu.datastore_update(self.server.context, pdu.dev_id)
            except (NoSuchIdException, KeyError):
                response = ExceptionResponse(pdu.function_code, ExcCodes.GATEWAY_NO_RESPONSE)
            except Exception:
                response = ExceptionResponse(pdu.function_code, ExcCodes.DEVICE_FAILURE)
            response.transaction_id = pdu.transaction_id
            response.dev_id = pdu.dev_id
            send_response = cast(
                Callable[[ModbusPDU, tuple[Any, ...] | None], None], self.server_send
            )
            send_response(response, addr)
        finally:
            _request_bit_span.reset(bit_token)
            _request_ip.reset(token)

    def callback_disconnected(self, exc: Exception | None) -> None:
        # 不再处理断开连接的排队请求；在途控制被取消时由 CommanderProxy
        # 将结果标记 UNKNOWN 并持久闭锁，不能静默继续执行队列。
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        while not self._request_queue.empty():
            self._request_queue.get_nowait()
        self._tcp_pending.clear()
        super().callback_disconnected(exc)


class GatewayTcpServer(ModbusTcpServer):
    def callback_new_connection(self) -> ServerRequestHandler:
        return _GatewayRequestHandler(self, self.trace_packet, self.trace_pdu, self.trace_connect)
