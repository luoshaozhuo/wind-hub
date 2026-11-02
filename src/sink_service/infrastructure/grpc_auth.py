"""mTLS peer identity authorization for management RPCs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import grpc

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


class PeerAuthorization(grpc.aio.ServerInterceptor):
    """Authorize the URI SAN of a mutually authenticated client certificate."""

    def __init__(self, callers: object) -> None:
        if not isinstance(callers, list) or not callers:
            raise ConfigurationError("security.callers must be a non-empty list")
        identities: dict[str, frozenset[str]] = {}
        for caller in callers:
            if not isinstance(caller, dict):
                raise ConfigurationError("invalid security caller")
            identity = caller.get("certificate_identity")
            permissions = caller.get("permissions")
            if (
                not isinstance(identity, str)
                or not identity.startswith("spiffe://")
                or not isinstance(permissions, list)
                or not permissions
                or set(permissions) - {"query", "control", "config"}
                or identity in identities
            ):
                raise ConfigurationError("invalid or duplicate caller identity")
            identities[identity] = frozenset(permissions)
        self._identities = identities

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler | None]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        handler = await continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler
        name = handler_call_details.method.rsplit("/", maxsplit=1)[-1]
        permission = _PERMISSIONS.get(name)
        if permission is None:
            return handler

        async def authorized(request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
            peer = context.auth_context() or {}
            identities = peer.get("x509_subject_alternative_name", ())
            allowed = any(
                permission in self._identities.get(
                    identity.decode("utf-8", errors="strict"), frozenset()
                )
                for identity in identities
            )
            if not allowed:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "not authorized")
            return await handler.unary_unary(request, context)

        return grpc.unary_unary_rpc_method_handler(
            authorized,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
