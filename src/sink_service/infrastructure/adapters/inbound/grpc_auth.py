"""Scoped bearer authorization interceptor for all Sink management RPCs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

import grpc

from core.infrastructure.grpc_bearer import (
    BearerAuthenticationError,
    BearerAuthorizationError,
    BearerAuthorizer,
    CallerGrant,
)
from sink_service.domain.config import ConfigurationError

_PERMISSIONS = {
    "GetServiceInfo": "query",
    "GetServiceStatus": "query",
    "GetCapabilities": "query",
    "GetConfigSnapshot": "query",
    "PushConfigSnapshot": "config",
    "ApplyConfigSnapshot": "config",
    "GetMetrics": "query",
    "GetEvents": "query",
    "Diagnose": "query",
    "ListDeliveries": "query",
    "GetDelivery": "query",
    "StartDelivery": "control",
    "StopDelivery": "control",
}

_CURRENT_CALLER: ContextVar[str] = ContextVar("sink_current_caller", default="")


def current_caller() -> str:
    """Return the caller ID verified by the Sink RPC interceptor."""
    return _CURRENT_CALLER.get() or "unidentified"


class PeerAuthorization(grpc.aio.ServerInterceptor):
    """Enforce declared operation grants for every registered Sink unary RPC."""

    def __init__(self, callers: object) -> None:
        if not isinstance(callers, list) or not callers:
            raise ConfigurationError("security.callers must be a non-empty list")
        grants: list[CallerGrant] = []
        for caller in callers:
            if not isinstance(caller, dict):
                raise ConfigurationError("invalid security caller")
            rights = caller.get("permissions")
            if not isinstance(rights, list):
                raise ConfigurationError("invalid security caller rights")
            grants.append(CallerGrant(
                caller_id=str(caller.get("caller_id", "")),
                token_env=str(caller.get("token_env", "")),
                permissions=frozenset(rights),
            ))
        self._authorizer = BearerAuthorizer(grants)

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        handler = await continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler
        name = handler_call_details.method.rsplit("/", maxsplit=1)[-1]
        permission = _PERMISSIONS.get(name)
        behavior: Callable[..., Any] = handler.unary_unary

        async def authorized(
            request: Any, context: grpc.aio.ServicerContext[Any, Any]
        ) -> Any:
            if permission is None:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "unknown RPC operation")
                raise RuntimeError("gRPC abort returned")
            try:
                actor = self._authorizer.require(context, permission)
            except BearerAuthenticationError as exc:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
                raise RuntimeError("gRPC abort returned") from exc
            except BearerAuthorizationError as exc:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
                raise RuntimeError("gRPC abort returned") from exc
            token = _CURRENT_CALLER.set(actor)
            try:
                return await behavior(request, context)
            finally:
                _CURRENT_CALLER.reset(token)

        return grpc.unary_unary_rpc_method_handler(
            authorized,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
