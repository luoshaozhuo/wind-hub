"""Per-caller Bearer authorization for internal Sink management RPCs."""

from __future__ import annotations

import hmac
import re
from collections.abc import Awaitable, Callable
from typing import Any

import grpc

from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.secrets import resolve_secret

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


class PeerAuthorization(grpc.aio.ServerInterceptor):
    """Fail closed on missing, duplicate, invalid or unauthorized Bearer tokens."""

    def __init__(self, callers: object) -> None:
        if not isinstance(callers, list) or not callers:
            raise ConfigurationError("security.callers must be a non-empty list")
        identities: dict[str, tuple[str, frozenset[str]]] = {}
        for caller in callers:
            if not isinstance(caller, dict) or set(caller) != {
                "caller_id", "token_env", "permissions"
            }:
                raise ConfigurationError("invalid security caller")
            caller_id = caller["caller_id"]
            reference = caller["token_env"]
            permissions = caller["permissions"]
            if (
                not isinstance(caller_id, str)
                or not caller_id.strip()
                or not isinstance(reference, str)
                or re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", reference) is None
                or not isinstance(permissions, list)
                or not permissions
                or any(value not in {"query", "control", "config"} for value in permissions)
                or len(permissions) != len(set(permissions))
                or caller_id in identities
            ):
                raise ConfigurationError("invalid or duplicate security caller")
            token = resolve_secret(reference)
            if token is None or len(token) < 32 or token.strip() != token:
                raise ConfigurationError("security caller token must contain at least 32 bytes")
            if any(hmac.compare_digest(token, old[0]) for old in identities.values()):
                raise ConfigurationError("security caller tokens must be unique")
            identities[caller_id] = (token, frozenset(permissions))
        self._identities = identities

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

        async def authorized(request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
            if permission is None:
                await context.abort(
                    grpc.StatusCode.PERMISSION_DENIED, "unsupported management RPC"
                )
            supplied = [
                item[1]
                for item in context.invocation_metadata()
                if item[0].lower() == "authorization"
            ]
            if len(supplied) != 1 or not isinstance(supplied[0], str):
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "valid Bearer token required")
            candidate = supplied[0][7:] if supplied[0].startswith("Bearer ") else ""
            permitted = any(
                hmac.compare_digest(candidate, token) and permission in roles
                for token, roles in self._identities.values()
            )
            if not permitted:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "not authorized")
            return await behavior(request, context)

        return grpc.unary_unary_rpc_method_handler(
            authorized,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
