"""Partial grpc.aio declarations for public interception and client call types."""

from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterable, Sequence
from typing import Any, Generic, NamedTuple, TypeVar

import grpc

_Request = TypeVar("_Request")
_Response = TypeVar("_Response")


class ClientCallDetails(NamedTuple):
    method: str | bytes
    timeout: float | None
    metadata: Sequence[tuple[str, str | bytes]] | None
    credentials: Any
    wait_for_ready: bool | None


class ServicerContext(Generic[_Request, _Response]):
    async def abort(self, code: grpc.StatusCode, details: str = ...) -> None: ...
    def invocation_metadata(self) -> Iterable[tuple[str, str | bytes]]: ...


class ServerInterceptor:
    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None: ...


class UnaryUnaryCall(Awaitable[_Response], Generic[_Request, _Response]):
    def __await__(self) -> Generator[Any, None, _Response]: ...
    async def code(self) -> grpc.StatusCode: ...
    def add_done_callback(self, callback: Callable[[UnaryUnaryCall[_Request, _Response]], None]) -> None: ...


class UnaryStreamCall(AsyncIterator[_Response], Generic[_Request, _Response]):
    def __aiter__(self) -> UnaryStreamCall[_Request, _Response]: ...
    async def __anext__(self) -> _Response: ...
    async def code(self) -> grpc.StatusCode: ...
    def add_done_callback(self, callback: Callable[[UnaryStreamCall[_Request, _Response]], None]) -> None: ...


class StreamUnaryCall(UnaryUnaryCall[_Request, _Response], Generic[_Request, _Response]): ...
class StreamStreamCall(UnaryStreamCall[_Request, _Response], Generic[_Request, _Response]): ...


class UnaryUnaryClientInterceptor:
    async def intercept_unary_unary(
        self,
        continuation: Callable[[ClientCallDetails, _Request], UnaryUnaryCall[_Request, _Response]],
        client_call_details: ClientCallDetails,
        request: _Request,
    ) -> UnaryUnaryCall[_Request, _Response] | _Response: ...


class UnaryStreamClientInterceptor:
    async def intercept_unary_stream(
        self,
        continuation: Callable[[ClientCallDetails, _Request], UnaryStreamCall[_Request, _Response]],
        client_call_details: ClientCallDetails,
        request: _Request,
    ) -> UnaryStreamCall[_Request, _Response] | AsyncIterator[_Response]: ...


class StreamUnaryClientInterceptor:
    async def intercept_stream_unary(
        self,
        continuation: Callable[[ClientCallDetails, AsyncIterator[_Request]], StreamUnaryCall[_Request, _Response]],
        client_call_details: ClientCallDetails,
        request_iterator: AsyncIterator[_Request],
    ) -> StreamUnaryCall[_Request, _Response]: ...


class StreamStreamClientInterceptor:
    async def intercept_stream_stream(
        self,
        continuation: Callable[[ClientCallDetails, AsyncIterator[_Request]], StreamStreamCall[_Request, _Response]],
        client_call_details: ClientCallDetails,
        request_iterator: AsyncIterator[_Request],
    ) -> StreamStreamCall[_Request, _Response] | AsyncIterator[_Response]: ...


class AioRpcError(grpc.RpcError):
    def code(self) -> grpc.StatusCode: ...
    def details(self) -> str: ...


class UnaryUnaryMultiCallable(Generic[_Request, _Response]):
    def __call__(
        self, request: _Request, *, timeout: float | None = ..., metadata: Any = ...
    ) -> UnaryUnaryCall[_Request, _Response]: ...


class UnaryStreamMultiCallable(Generic[_Request, _Response]): ...
class StreamUnaryMultiCallable(Generic[_Request, _Response]): ...
class StreamStreamMultiCallable(Generic[_Request, _Response]): ...


class Channel:
    async def close(self, grace: float | None = ...) -> None: ...
    def unary_unary(self, method: str, **kwargs: Any) -> Any: ...
    def unary_stream(self, method: str, **kwargs: Any) -> Any: ...
    def stream_unary(self, method: str, **kwargs: Any) -> Any: ...
    def stream_stream(self, method: str, **kwargs: Any) -> Any: ...


class Server:
    async def start(self) -> None: ...
    async def stop(self, grace: float | None) -> None: ...
    async def wait_for_termination(self, timeout: float | None = ...) -> bool: ...
    def add_insecure_port(self, address: str) -> int: ...
    def add_generic_rpc_handlers(self, handlers: Any) -> None: ...
    def add_registered_method_handlers(self, service_name: str, method_handlers: Any) -> None: ...
    def add_secure_port(self, address: str, server_credentials: Any) -> int: ...


def insecure_channel(
    target: str,
    *,
    interceptors: Sequence[
        UnaryUnaryClientInterceptor
        | UnaryStreamClientInterceptor
        | StreamUnaryClientInterceptor
        | StreamStreamClientInterceptor
    ] | None = ...,
    options: Any = ...,
    compression: Any = ...,
) -> Channel: ...


def __getattr__(name: str) -> Any: ...
