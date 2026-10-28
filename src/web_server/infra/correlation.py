"""Request-ID 关联：HTTP 入口 → 日志 → gRPC 出站的统一链路。

- contextvar、过滤器与规整逻辑由 ``core.infrastructure.diagnostic.correlation``
  统一持有（Collector/Commander 入站拦截器共用同一实现），本模块做再导出
  与 HTTP/gRPC 客户端传输适配。
- ``RequestIdMiddleware`` 在 HTTP 边界接收/生成 Request-ID，写入响应头。
- ``RequestIdInterceptor`` 把当前 Request-ID 作为 gRPC metadata 传给
  Collector/Commander，实现跨进程串联。

不涉及任何鉴权语义；Request-ID 仅用于可观测性。
"""

from __future__ import annotations

import uuid
from typing import Any

import grpc
from starlette.types import ASGIApp, Receive, Scope, Send

from core.infrastructure.diagnostic.correlation import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    get_request_id,
    normalize_request_id,
    request_id_var,
)

__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIdFilter",
    "RequestIdInterceptor",
    "RequestIdMiddleware",
    "get_request_id",
    "request_id_var",
]


class RequestIdMiddleware:
    """纯 ASGI 中间件：为每个 HTTP 请求建立 Request-ID 上下文。

    客户端携带 ``x-request-id`` 时透传（规整/截断到 128 字符防滥用），
    否则生成 UUID4。响应始终回写该头，便于调用方关联。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        raw_headers = scope.get("headers", [])
        headers = {k.decode().lower(): v.decode(errors="replace") for k, v in raw_headers}
        request_id = normalize_request_id(headers.get(REQUEST_ID_HEADER)) or uuid.uuid4().hex
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
