"""Request-ID 关联：HTTP 入口 → 日志 → gRPC 出站的统一链路。

- ``request_id_var`` 是进程内唯一的传递载体（contextvar，协程安全）。
- ``RequestIdMiddleware`` 在 HTTP 边界接收/生成 Request-ID，写入响应头。
- ``RequestIdFilter`` 给日志记录注入 ``request_id`` 字段，现场排障可按其检索。
- ``RequestIdInterceptor`` 把当前 Request-ID 作为 gRPC metadata 传给
  Collector/Commander，实现跨进程串联。

不涉及任何鉴权语义；Request-ID 仅用于可观测性。
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Any

import grpc
from starlette.types import ASGIApp, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """返回当前请求上下文的 Request-ID；无上下文时为空串。"""
    return request_id_var.get()


class RequestIdMiddleware:
    """纯 ASGI 中间件：为每个 HTTP 请求建立 Request-ID 上下文。

    客户端携带 ``x-request-id`` 时透传（截断到 128 字符防滥用），否则生成
    UUID4。响应始终回写该头，便于调用方关联。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        request_id = headers.get(REQUEST_ID_HEADER, "").strip()[:128] or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            await self._app(scope, receive, _request_id_send(send, request_id))
        finally:
            request_id_var.reset(token)


def _request_id_send(send: Send, request_id: str) -> Send:
    async def wrapper(message: Any) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
            message = {**message, "headers": headers}
        await send(message)

    return wrapper


class RequestIdFilter(logging.Filter):
    """把当前 Request-ID 注入日志记录；无请求上下文时为 ``-``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


# grpc.aio 的客户端拦截器基类在类型存根中为 Any。
class RequestIdInterceptor(grpc.aio.UnaryUnaryClientInterceptor):  # type: ignore[misc]
    """把当前 Request-ID 作为 metadata 附加到所有 unary 出站 RPC。"""

    async def intercept_unary_unary(
        self,
        continuation: Any,
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        request_id = get_request_id()
        if request_id:
            metadata = list(client_call_details.metadata or [])
            metadata.append((REQUEST_ID_HEADER, request_id))
            client_call_details = client_call_details._replace(metadata=metadata)
        return await continuation(client_call_details, request)
