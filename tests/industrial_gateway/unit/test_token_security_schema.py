"""Strict token-only Gateway Service v2/v3 configuration admission."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from industrial_gateway.infrastructure.config.schema import GatewayServiceConfig

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "name",
    [
        "service_gateway-sim_v1.3.0.yaml",
        "service_gateway-e2e-source_v1.1.2.yaml",
        "service_gateway-e2e-target_v1.1.1.yaml",
    ],
)
def test_tokenized_gateway_service_candidates_have_strict_schema(name: str) -> None:
    source = ROOT / "configs/services" / name
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    config = GatewayServiceConfig.model_validate(raw)
    assert config.schema_version in {2, 3}
    assert config.security.callers
    assert config.grpc.listen_host == "127.0.0.1"


def _valid() -> dict:
    source = ROOT / "configs/services/service_gateway-e2e-source_v1.1.2.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


@pytest.mark.parametrize(
    "mutator",
    [
        lambda doc: doc["grpc"].update({"permissions": {"operator": ["read"]}}),
        lambda doc: doc["grpc"].update({"tls": {"enabled": False}}),
        lambda doc: doc["grpc"].update({"listen_host": "0.0.0.0"}),
        lambda doc: doc.pop("security"),
        lambda doc: doc["security"]["callers"][0].pop("token_env"),
        lambda doc: doc["security"]["callers"][0].update({"token_env": "env:BAD"}),
        lambda doc: doc["servers"][-1]["control"]["target"].pop("token_env"),
        lambda doc: doc["servers"][-1]["control"]["simulation"].pop("token_env"),
    ],
)
def test_gateway_security_contract_rejects_invalid_candidate(
    mutator: Callable[[dict[str, Any]], object],
) -> None:
    raw = deepcopy(_valid())
    mutator(raw)
    with pytest.raises(ValidationError):
        GatewayServiceConfig.model_validate(raw)


def test_sink_listener_requires_distinct_port_and_caller() -> None:
    raw = _valid()
    raw["grpc"]["sink_listener_port"] = raw["grpc"]["listen_port"]
    with pytest.raises(ValidationError, match="listener"):
        GatewayServiceConfig.model_validate(raw)


def test_duplicate_caller_token_reference_is_rejected() -> None:
    raw = _valid()
    raw["security"]["callers"][1]["token_env"] = raw["security"]["callers"][0]["token_env"]
    with pytest.raises(ValidationError, match="duplicate"):
        GatewayServiceConfig.model_validate(raw)


@pytest.mark.parametrize("bad", [0, -1, True, False, 5000.0, "5000", None])
def test_read_stale_after_ms_rejects_invalid_values(bad: object) -> None:
    """零、负数、布尔、非整数期限必须被配置校验拒绝；None 表示未配置。"""
    raw = _valid()
    raw["servers"][-1]["read_stale_after_ms"] = bad
    if bad is None:
        config = GatewayServiceConfig.model_validate(raw)
        assert config.servers[-1].read_stale_after_ms is None
    else:
        with pytest.raises(ValidationError):
            GatewayServiceConfig.model_validate(raw)
