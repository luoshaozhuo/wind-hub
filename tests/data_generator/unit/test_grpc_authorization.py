"""All Data Generator and Common RPCs use the same fail-closed Bearer policy."""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from data_generator.infrastructure.adapters.inbound.grpc_auth import GeneratorAuthorization
from tests.support.gateway import reserve_port

pytestmark = [pytest.mark.unit, pytest.mark.real_service]

_TOKEN = "a" * 40


async def _echo(request: bytes, context: Any) -> bytes:
    del context
    return request


@pytest.mark.parametrize(
    ("method", "token", "permissions", "expected"),
    [
        ("GetServiceInfo", _TOKEN, {"variables_read"}, grpc.StatusCode.OK),
        ("GetServiceInfo", "", {"variables_read"}, grpc.StatusCode.UNAUTHENTICATED),
        ("StartGeneration", _TOKEN, {"variables_read"}, grpc.StatusCode.PERMISSION_DENIED),
        ("StartGeneration", _TOKEN, {"variables_update"}, grpc.StatusCode.PERMISSION_DENIED),
        ("StopGeneration", _TOKEN, {"variables_update"}, grpc.StatusCode.PERMISSION_DENIED),
        ("StartGeneration", _TOKEN, {"generation_manage"}, grpc.StatusCode.OK),
        ("StopGeneration", _TOKEN, {"generation_manage"}, grpc.StatusCode.OK),
        ("UpdateVariables", _TOKEN, {"generation_manage"}, grpc.StatusCode.PERMISSION_DENIED),
        ("PushConfigSnapshot", _TOKEN, {"variables_read"}, grpc.StatusCode.PERMISSION_DENIED),
        ("GetInputExecution", _TOKEN, {"variables_read"}, grpc.StatusCode.PERMISSION_DENIED),
        ("GetInputExecution", _TOKEN, {"variables_update"}, grpc.StatusCode.OK),
    ],
)
async def test_all_rpc_methods_require_scope(
    method: str, token: str, permissions: set[str], expected: grpc.StatusCode
) -> None:
    interceptor = GeneratorAuthorization({_TOKEN: ("caller", frozenset(permissions))})
    server = grpc.aio.server(interceptors=[interceptor])
    handler = grpc.unary_unary_rpc_method_handler(
        _echo, request_deserializer=lambda value: value, response_serializer=lambda value: value
    )
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "windhub.common.v1.CommonService",
                {
                    name: handler
                    for name in (
                        "GetServiceInfo",
                        "StartGeneration",
                        "StopGeneration",
                        "UpdateVariables",
                        "PushConfigSnapshot",
                        "GetInputExecution",
                    )
                },
            ),
        )
    )
    port = reserve_port()
    assert server.add_insecure_port(f"127.0.0.1:{port}")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            call = channel.unary_unary(
                f"/windhub.common.v1.CommonService/{method}",
                request_serializer=lambda value: value,
                response_deserializer=lambda value: value,
            )
            metadata = (("authorization", f"Bearer {token}"),) if token else ()
            if expected == grpc.StatusCode.OK:
                assert await call(b"payload", metadata=metadata) == b"payload"
            else:
                with pytest.raises(grpc.aio.AioRpcError) as exc:
                    await call(b"payload", metadata=metadata)
                assert exc.value.code() == expected
    finally:
        await server.stop(0)
