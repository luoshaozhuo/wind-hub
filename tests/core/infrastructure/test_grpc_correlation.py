"""gRPC 入站 Request-ID 拦截器测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import grpc

from core.infrastructure.diagnostic.correlation import get_request_id
from core.infrastructure.rpc.correlation import RequestIdServerInterceptor


def _details(metadata: Any) -> SimpleNamespace:
    return SimpleNamespace(invocation_metadata=metadata)


async def _continuation_factory(handler: grpc.RpcMethodHandler | None) -> Any:
    async def continuation(details: Any) -> grpc.RpcMethodHandler | None:
        return handler

    return continuation


async def test_unary_unary_binds_metadata_request_id() -> None:
    seen: dict[str, str] = {}

    async def echo(request: Any, context: Any) -> str:
        seen["id"] = get_request_id()
        return "ok"

    handler = grpc.unary_unary_rpc_method_handler(echo)
    continuation = await _continuation_factory(handler)

    wrapped = await RequestIdServerInterceptor().intercept_service(
        continuation,
        _details((("X-Request-Id", "rpc-trace-1"),)),
    )
    assert wrapped is not None
    result = await wrapped.unary_unary("req", None)
    assert result == "ok"
    assert seen["id"] == "rpc-trace-1"
    assert get_request_id() == ""


async def test_generates_id_when_absent() -> None:
    seen: dict[str, str] = {}

    async def echo(request: Any, context: Any) -> None:
        seen["id"] = get_request_id()

    handler = grpc.unary_unary_rpc_method_handler(echo)
    continuation = await _continuation_factory(handler)

    wrapped = await RequestIdServerInterceptor().intercept_service(continuation, _details(()))
    assert wrapped is not None
    await wrapped.unary_unary("req", None)
    assert len(seen["id"]) == 32


async def test_none_handler_passes_through() -> None:
    continuation = await _continuation_factory(None)
    wrapped = await RequestIdServerInterceptor().intercept_service(
        continuation, _details(())
    )
    assert wrapped is None


async def test_stream_unary_binds_request_id() -> None:
    seen: dict[str, str] = {}

    async def collect(request_iterator: Any, context: Any) -> int:
        seen["id"] = get_request_id()
        total = 0
        async for value in request_iterator:
            total += value
        return total

    handler = grpc.stream_unary_rpc_method_handler(collect)
    continuation = await _continuation_factory(handler)

    wrapped = await RequestIdServerInterceptor().intercept_service(
        continuation,
        _details((("x-request-id", "stream-9"),)),
    )
    assert wrapped is not None

    async def items() -> Any:
        for value in (1, 2, 3):
            yield value

    assert await wrapped.stream_unary(items(), None) == 6
    assert seen["id"] == "stream-9"


async def test_unary_stream_binds_request_id() -> None:
    seen: dict[str, str] = {}

    async def numbers(request: Any, context: Any) -> Any:
        seen["id"] = get_request_id()
        for value in (1, 2):
            yield value

    handler = grpc.unary_stream_rpc_method_handler(numbers)
    continuation = await _continuation_factory(handler)

    wrapped = await RequestIdServerInterceptor().intercept_service(
        continuation, _details((("x-request-id", "srv-3"),))
    )
    assert wrapped is not None
    out: list[int] = []
    async for value in wrapped.unary_stream("req", None):
        out.append(value)
    assert out == [1, 2]
    assert seen["id"] == "srv-3"


async def test_stream_stream_binds_request_id() -> None:
    seen: dict[str, str] = {}

    async def double(request_iterator: Any, context: Any) -> Any:
        seen["id"] = get_request_id()
        async for value in request_iterator:
            yield value * 2

    handler = grpc.stream_stream_rpc_method_handler(double)
    continuation = await _continuation_factory(handler)

    wrapped = await RequestIdServerInterceptor().intercept_service(
        continuation, _details((("x-request-id", "ss-5"),))
    )
    assert wrapped is not None

    async def items() -> Any:
        for value in (1, 2):
            yield value

    out: list[int] = []
    async for value in wrapped.stream_stream(items(), None):
        out.append(value)
    assert out == [2, 4]
    assert seen["id"] == "ss-5"
