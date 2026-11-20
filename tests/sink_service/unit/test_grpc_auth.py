"""Unit tests for the Sink PeerAuthorization gRPC interceptor.

Covers fail-closed caller configuration, missing/duplicate/invalid bearer
tokens, insufficient permissions, unknown RPC rejection and a successful
authorized call, without standing up a real gRPC server.
"""

from __future__ import annotations

import asyncio
from typing import Any

import grpc
import pytest

from sink_service.infrastructure.adapters.inbound.grpc_auth import (
    PeerAuthorization,
)

pytestmark = pytest.mark.unit

VALID_TOKEN = "sink-management-token-0123456789abcdef"
OTHER_TOKEN = "sink-other-valid-token-0123456789abcd"


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WIND_HUB_SINK_CLI_TOKEN", VALID_TOKEN)
    monkeypatch.setenv("WIND_HUB_SINK_WEB_TOKEN", OTHER_TOKEN)


def _callers() -> list[dict[str, str]]:
    return [
        {
            "caller_id": "cli",
            "token_env": "env:WIND_HUB_SINK_CLI_TOKEN",
            "permissions": ["query", "control", "config"],
        },
        {
            "caller_id": "web",
            "token_env": "env:WIND_HUB_SINK_WEB_TOKEN",
            "permissions": ["query"],
        },
    ]


class _AbortError(Exception):
    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self.code = code


class _Context:
    def __init__(self, metadata: list[tuple[str, str]] | None = None) -> None:
        self._metadata = metadata or []

    def invocation_metadata(self) -> list[tuple[str, str]]:
        return self._metadata

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        raise _AbortError(code, details)


class _RawHandler:
    def __init__(self) -> None:
        self.called = False

    async def behavior(self, request: Any, context: Any) -> str:
        self.called = True
        return "ok"


def _details(method: str) -> grpc.HandlerCallDetails:
    return type("Details", (), {"method": f"/sink_service.SinkService/{method}"})()


async def _invoke(interceptor: PeerAuthorization, method: str, metadata):
    raw = _RawHandler()
    raw_handler = grpc.unary_unary_rpc_method_handler(
        raw.behavior,
        request_deserializer=lambda data: data,
        response_serializer=lambda message: message,
    )

    def continuation(_details: grpc.HandlerCallDetails) -> Any:
        future: Any = asyncio.Future()
        future.set_result(raw_handler)
        return future

    wrapped = await interceptor.intercept_service(continuation, _details(method))
    return wrapped, raw


@pytest.mark.parametrize(
    "callers",
    [
        [],  # empty caller list
        [{"caller_id": "x", "token_env": "WIND_HUB_SINK_CLI_TOKEN", "permissions": ["query"]}],
        [{"caller_id": "x", "token_env": "env:WIND_HUB_SINK_CLI_TOKEN", "permissions": ["root"]}],
        [
            {
                "caller_id": "dup",
                "token_env": "env:WIND_HUB_SINK_CLI_TOKEN",
                "permissions": ["query"],
            },
            {
                "caller_id": "dup",
                "token_env": "env:WIND_HUB_SINK_WEB_TOKEN",
                "permissions": ["query"],
            },
        ],
        [
            {
                "caller_id": "a",
                "token_env": "env:WIND_HUB_SINK_CLI_TOKEN",
                "permissions": ["query"],
            },
            {
                "caller_id": "b",
                "token_env": "env:WIND_HUB_SINK_CLI_TOKEN",
                "permissions": ["query"],
            },
        ],
    ],
)
def test_invalid_caller_configuration_is_rejected_at_construction(callers) -> None:
    with pytest.raises(Exception):  # noqa: B017, BLE001 - fail-closed construction
        PeerAuthorization(callers)


@pytest.mark.asyncio
async def test_missing_token_is_unauthenticated() -> None:
    interceptor = PeerAuthorization(_callers())
    wrapped, raw = await _invoke(interceptor, "ListDeliveries", [])
    with pytest.raises(_AbortError) as exc:
        await wrapped.unary_unary(None, _Context(metadata=[]))
    assert exc.value.code == grpc.StatusCode.UNAUTHENTICATED
    assert not raw.called


@pytest.mark.asyncio
async def test_duplicate_authorization_header_is_unauthenticated() -> None:
    interceptor = PeerAuthorization(_callers())
    metadata = [
        ("authorization", f"Bearer {VALID_TOKEN}"),
        ("authorization", f"Bearer {VALID_TOKEN}"),
    ]
    wrapped, _raw = await _invoke(interceptor, "ListDeliveries", metadata)
    with pytest.raises(_AbortError) as exc:
        await wrapped.unary_unary(None, _Context(metadata=metadata))
    assert exc.value.code == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_wrong_token_is_denied() -> None:
    # A well-formed but non-matching Bearer token passes the single-header
    # check but fails credential/permission comparison -> PERMISSION_DENIED.
    interceptor = PeerAuthorization(_callers())
    metadata = [("authorization", "Bearer definitely-wrong-secret-value")]
    wrapped, _raw = await _invoke(interceptor, "ListDeliveries", metadata)
    with pytest.raises(_AbortError) as exc:
        await wrapped.unary_unary(None, _Context(metadata=metadata))
    assert exc.value.code == grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_query_only_caller_is_denied_control_permission() -> None:
    interceptor = PeerAuthorization(_callers())
    # The web caller only has "query"; StartDelivery requires "control".
    metadata = [("authorization", f"Bearer {OTHER_TOKEN}")]
    wrapped, _raw = await _invoke(interceptor, "StartDelivery", metadata)
    with pytest.raises(_AbortError) as exc:
        await wrapped.unary_unary(None, _Context(metadata=metadata))
    assert exc.value.code == grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_unknown_rpc_is_denied_even_with_a_valid_token() -> None:
    interceptor = PeerAuthorization(_callers())
    metadata = [("authorization", f"Bearer {VALID_TOKEN}")]
    wrapped, _raw = await _invoke(interceptor, "UndocumentedRpc", metadata)
    with pytest.raises(_AbortError) as exc:
        await wrapped.unary_unary(None, _Context(metadata=metadata))
    assert exc.value.code == grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_authorized_call_reaches_handler() -> None:
    interceptor = PeerAuthorization(_callers())
    metadata = [("authorization", f"Bearer {VALID_TOKEN}")]
    wrapped, raw = await _invoke(interceptor, "ListDeliveries", metadata)
    result = await wrapped.unary_unary(None, _Context(metadata=metadata))
    assert result == "ok"
    assert raw.called
