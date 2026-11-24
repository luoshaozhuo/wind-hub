"""Per-RPC Bearer authorization for the Data Generator management listener."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any, cast

import grpc

from data_generator.domain.model import GeneratorError

_READ_METHODS = frozenset(
    {
        "ListDevices",
        "GetDevice",
        "ListScenarios",
        "GetGeneration",
        "ListVariables",
        "GetServiceInfo",
        "GetServiceStatus",
        "GetCapabilities",
        "GetConfigSnapshot",
        "GetMetrics",
        "GetEvents",
        "Diagnose",
    }
)
_UPDATE_METHODS = frozenset(
    {
        "StartGeneration",
        "StopGeneration",
        "UpdateVariables",
        "GetInputExecution",
        "PushConfigSnapshot",
        "ApplyConfigSnapshot",
    }
)


class GeneratorAuthorization(grpc.aio.ServerInterceptor):
    """Fail closed for every unary RPC, including CommonService methods."""

    def __init__(self, identities: dict[str, tuple[str, frozenset[str]]]) -> None:
        if not identities:
            raise GeneratorError("Generator caller identities must be configured")
        self._identities = identities

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails],
            Awaitable[grpc.RpcMethodHandler[Any, Any] | None],
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        handler = await continuation(handler_call_details)
        if handler is None:
            return None
        unary_unary = handler.unary_unary
        if unary_unary is None:
            return handler
        method = handler_call_details.method.rsplit("/", maxsplit=1)[-1]
        if method in _READ_METHODS:
            required = "variables_read"
        elif method in _UPDATE_METHODS:
            required = "variables_update"
        else:
            required = ""

        async def authorized(
            request: Any,
            context: grpc.aio.ServicerContext[Any, Any],
        ) -> Any:
            if not required:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "unknown method")
                raise RuntimeError("gRPC abort unexpectedly returned")
            if not context.peer().startswith(("ipv4:127.0.0.1:", "ipv6:[::1]:")):
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "loopback only")
                raise RuntimeError("gRPC abort unexpectedly returned")
            metadata = context.invocation_metadata()
            supplied = (
                [value for key, value in metadata if key.lower() == "authorization"]
                if metadata
                else []
            )
            if len(supplied) != 1 or not isinstance(supplied[0], str):
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "one Bearer token required")
                raise RuntimeError("gRPC abort unexpectedly returned")
            authorization = supplied[0]
            token = authorization[7:] if authorization.startswith("Bearer ") else ""
            matched: tuple[str, frozenset[str]] | None = None
            if token:
                for secret, identity in self._identities.items():
                    if hmac.compare_digest(token, secret):
                        matched = identity
            if matched is None:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "unknown Bearer token")
                raise RuntimeError("gRPC abort unexpectedly returned")
            if required not in matched[1]:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "caller lacks permission")
                raise RuntimeError("gRPC abort unexpectedly returned")
            return await unary_unary(request, cast(grpc.ServicerContext, context))

        return grpc.unary_unary_rpc_method_handler(
            authorized,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
