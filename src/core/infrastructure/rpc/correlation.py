"""gRPC 入站 Request-ID 拦截器：metadata → contextvar。

Web Server 的出站客户端拦截器把 ``x-request-id`` 附到 metadata；
Collector/Commander 服务端在此读取并绑定到日志上下文，实现跨进程
同一请求串联。未携带时生成新 ID，使每次 RPC 仍可独立检索。

``grpc.aio`` 服务端只调用 ``intercept_service``（``intercept_unary_*``
系列方法属于同步 ``grpc.ServerInterceptor``，在 aio server 上不会生效），
因此按四种流模式包装 RpcMethodHandler 的 behavior。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import grpc

from core.infrastructure.diagnostic.correlation import (
    REQUEST_ID_HEADER,
    bind_request_id,
    new_request_id,
    normalize_request_id,
)


def _metadata_request_id(metadata: Any) -> str:
    if not metadata:
        return new_request_id()
    for key, value in metadata:
        if key.lower() == REQUEST_ID_HEADER:
            request_id = normalize_request_id(str(value))
            return request_id or new_request_id()
    return new_request_id()


def _wrap_unary_unary(behavior: Callable[..., Any], request_id: str) -> Callable[..., Any]:
    async def wrapper(request: Any, context: grpc.aio.ServicerContext) -> Any:
        with bind_request_id(request_id):
            return await behavior(request, context)

    return wrapper


def _wrap_unary_stream(behavior: Callable[..., Any], request_id: str) -> Callable[..., Any]:
    async def wrapper(request: Any, context: grpc.aio.ServicerContext) -> AsyncIterator[Any]:
        with bind_request_id(request_id):
            async for response in behavior(request, context):
                yield response

    return wrapper


def _wrap_stream_unary(behavior: Callable[..., Any], request_id: str) -> Callable[..., Any]:
    async def wrapper(request_iterator: Any, context: grpc.aio.ServicerContext) -> Any:
        with bind_request_id(request_id):
            return await behavior(request_iterator, context)

    return wrapper


def _wrap_stream_stream(behavior: Callable[..., Any], request_id: str) -> Callable[..., Any]:
    async def wrapper(
        request_iterator: Any, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[Any]:
        with bind_request_id(request_id):
            async for response in behavior(request_iterator, context):
                yield response

    return wrapper


class RequestIdServerInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    """包装四种流模式的 handler，在业务执行期间绑定 Request-ID。"""

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        handler = await continuation(handler_call_details)
        if handler is None:
            return None
        request_id = _metadata_request_id(handler_call_details.invocation_metadata)
        factory: Callable[..., grpc.RpcMethodHandler]
        if handler.request_streaming:
            if handler.response_streaming:
                factory = grpc.stream_stream_rpc_method_handler
                behavior = _wrap_stream_stream(handler.stream_stream, request_id)
            else:
                factory = grpc.stream_unary_rpc_method_handler
                behavior = _wrap_stream_unary(handler.stream_unary, request_id)
        elif handler.response_streaming:
            factory = grpc.unary_stream_rpc_method_handler
            behavior = _wrap_unary_stream(handler.unary_stream, request_id)
        else:
            factory = grpc.unary_unary_rpc_method_handler
            behavior = _wrap_unary_unary(handler.unary_unary, request_id)
        return factory(
            behavior,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
