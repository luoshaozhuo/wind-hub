"""Repository-local partial grpcio type declarations for typed async interception."""

from collections.abc import Awaitable, Callable, Iterable, Iterator
from enum import Enum
from typing import Any, Generic, TypeVar

_Request = TypeVar("_Request")
_Response = TypeVar("_Response")


class StatusCode(Enum):
    OK: StatusCode
    CANCELLED: StatusCode
    UNKNOWN: StatusCode
    INVALID_ARGUMENT: StatusCode
    DEADLINE_EXCEEDED: StatusCode
    NOT_FOUND: StatusCode
    ALREADY_EXISTS: StatusCode
    PERMISSION_DENIED: StatusCode
    RESOURCE_EXHAUSTED: StatusCode
    FAILED_PRECONDITION: StatusCode
    ABORTED: StatusCode
    OUT_OF_RANGE: StatusCode
    UNIMPLEMENTED: StatusCode
    INTERNAL: StatusCode
    UNAVAILABLE: StatusCode
    DATA_LOSS: StatusCode
    UNAUTHENTICATED: StatusCode


class RpcError(Exception):
    def code(self) -> StatusCode: ...
    def details(self) -> str | None: ...


class HandlerCallDetails:
    method: str
    invocation_metadata: Iterable[tuple[str, str | bytes]] | None


class RpcMethodHandler(Generic[_Request, _Response]):
    request_streaming: bool
    response_streaming: bool
    unary_unary: Callable[..., Any]
    unary_stream: Callable[..., Any]
    stream_unary: Callable[..., Any]
    stream_stream: Callable[..., Any]
    request_deserializer: Callable[[bytes], _Request] | None
    response_serializer: Callable[[_Response], bytes] | None


class ServicerContext(Generic[_Request, _Response]): ...


class Channel: ...
class Server: ...


class UnaryUnaryMultiCallable(Generic[_Request, _Response]): ...
class UnaryStreamMultiCallable(Generic[_Request, _Response]): ...
class StreamUnaryMultiCallable(Generic[_Request, _Response]): ...
class StreamStreamMultiCallable(Generic[_Request, _Response]): ...


def unary_unary_rpc_method_handler(
    behavior: Callable[..., Any],
    request_deserializer: Callable[[bytes], Any] | None = ...,
    response_serializer: Callable[[Any], bytes] | None = ...,
) -> RpcMethodHandler[Any, Any]: ...
def unary_stream_rpc_method_handler(
    behavior: Callable[..., Any],
    request_deserializer: Callable[[bytes], Any] | None = ...,
    response_serializer: Callable[[Any], bytes] | None = ...,
) -> RpcMethodHandler[Any, Any]: ...
def stream_unary_rpc_method_handler(
    behavior: Callable[..., Any],
    request_deserializer: Callable[[bytes], Any] | None = ...,
    response_serializer: Callable[[Any], bytes] | None = ...,
) -> RpcMethodHandler[Any, Any]: ...
def stream_stream_rpc_method_handler(
    behavior: Callable[..., Any],
    request_deserializer: Callable[[bytes], Any] | None = ...,
    response_serializer: Callable[[Any], bytes] | None = ...,
) -> RpcMethodHandler[Any, Any]: ...


def __getattr__(name: str) -> Any: ...
