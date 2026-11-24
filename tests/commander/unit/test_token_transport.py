"""Commander token transport and authorization boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml

from commander.infrastructure.adapters.inbound.grpc.server import _Authorized
from commander.infrastructure.adapters.inbound.grpc_transport import _security_config
from commander.infrastructure.config.service_settings import validate_transport_settings

pytestmark = pytest.mark.unit

_TOKEN = "a" * 40


def _config() -> dict[str, Any]:
    return {
        "schema_version": 3,
        "grpc": {"listen_host": "127.0.0.1", "listen_port": 53052, "max_active_rpcs": 20},
        "security": {
            "callers": [
                {
                    "caller_id": "operator",
                    "token_env": "WIND_HUB_TEST_COMMANDER_OPERATOR_TOKEN",
                    "permissions": ["query", "control"],
                },
                {
                    "caller_id": "viewer",
                    "token_env": "WIND_HUB_TEST_COMMANDER_VIEWER_TOKEN",
                    "permissions": ["query"],
                },
            ]
        },
    }


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "service.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_token_transport_resolves_unique_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _config()
    monkeypatch.setenv("WIND_HUB_TEST_COMMANDER_OPERATOR_TOKEN", _TOKEN)
    monkeypatch.setenv("WIND_HUB_TEST_COMMANDER_VIEWER_TOKEN", "b" * 40)
    host, port, maximum, permissions = _security_config(_write(tmp_path, document))
    assert (host, port, maximum) == ("127.0.0.1", 53052, 20)
    assert permissions[_TOKEN] == ("operator", frozenset({"query", "control"}))
    assert permissions["b" * 40] == ("viewer", frozenset({"query"}))


@pytest.mark.parametrize(
    "bad_host",
    ["0.0.0.0", "192.168.1.20", "::", ""],
)
def test_non_loopback_commander_listener_is_rejected(bad_host: str) -> None:
    document = _config()
    document["grpc"]["listen_host"] = bad_host
    with pytest.raises(ValueError, match="loopback"):
        validate_transport_settings(document)


@pytest.mark.parametrize(
    "invalid",
    ["", "short", "a" * 35 + "\n", "a" * 35 + " ", "a" * 34 + "á"],
)
def test_caller_token_fails_closed(
    invalid: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIND_HUB_TEST_COMMANDER_OPERATOR_TOKEN", invalid)
    monkeypatch.setenv("WIND_HUB_TEST_COMMANDER_VIEWER_TOKEN", "b" * 40)
    with pytest.raises(ValueError, match="token"):
        _security_config(_write(tmp_path, _config()))


def test_duplicate_secret_values_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIND_HUB_TEST_COMMANDER_OPERATOR_TOKEN", _TOKEN)
    monkeypatch.setenv("WIND_HUB_TEST_COMMANDER_VIEWER_TOKEN", _TOKEN)
    with pytest.raises(ValueError, match="duplicate"):
        _security_config(_write(tmp_path, _config()))


@pytest.mark.parametrize("field", ["ca_cert", "server_cert", "certificate_identity"])
def test_legacy_certificate_fields_are_rejected(field: str) -> None:
    document = _config()
    if field == "certificate_identity":
        document["security"]["callers"][0][field] = "spiffe://wind-hub/operator"
    else:
        document["security"][field] = "/tmp/obsolete.pem"
    with pytest.raises(ValueError, match="unexpected"):
        validate_transport_settings(document)


class _AbortError(Exception):
    def __init__(self, code: grpc.StatusCode) -> None:
        self.code = code


class _Context:
    def __init__(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self._metadata = metadata

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        del details
        raise _AbortError(code)


@pytest.mark.parametrize(
    ("metadata", "scope", "expected_code"),
    [
        ((), "query", grpc.StatusCode.UNAUTHENTICATED),
        ((("authorization", "Bearer invalid"),), "query", grpc.StatusCode.UNAUTHENTICATED),
        ((("authorization", f"Bearer {_TOKEN}"),) * 2, "query", grpc.StatusCode.UNAUTHENTICATED),
        ((("authorization", f"Bearer {_TOKEN}"),), "recover", grpc.StatusCode.PERMISSION_DENIED),
    ],
)
async def test_commander_authorization_rejects_invalid_callers(
    metadata: tuple[tuple[str, str], ...],
    scope: str,
    expected_code: grpc.StatusCode,
) -> None:
    boundary = _Authorized(
        None,  # type: ignore[arg-type]
        {_TOKEN: ("operator", frozenset({"query", "control"}))},
    )
    with pytest.raises(_AbortError) as exc:
        await boundary.require(_Context(metadata), scope)  # type: ignore[arg-type]
    assert exc.value.code == expected_code


async def test_commander_authorization_resolves_caller_identity() -> None:
    boundary = _Authorized(
        None,  # type: ignore[arg-type]
        {_TOKEN: ("operator", frozenset({"query", "control"}))},
    )
    caller_id = await boundary.require(
        _Context((("authorization", f"Bearer {_TOKEN}"),)),  # type: ignore[arg-type]
        "control",
    )
    assert caller_id == "operator"
