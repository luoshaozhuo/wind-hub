"""Modbus 请求来源上下文：仅在单个请求的协程中传播客户端 IP。"""

from __future__ import annotations

from contextvars import ContextVar
from ipaddress import ip_address

from pymodbus.server import ModbusTcpServer
from pymodbus.server.requesthandler import ServerRequestHandler

_request_ip: ContextVar[str | None] = ContextVar("gateway_request_ip", default=None)


def request_ip() -> str | None:
    """只允许协议请求执行期间访问，不把来源身份保存在全局对象上。"""
    return _request_ip.get()


class _GatewayRequestHandler(ServerRequestHandler):
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
