"""OpenAPI 生成器需使用当前 Web Server，生成结果应可重复。"""

import json

from scripts.generate_api_contract import render


def test_openapi_generator_contains_new_rpc_routes() -> None:
    schema = json.loads(render())
    paths = schema["paths"]
    assert "/api/v1/collector/latest-values" in paths
    assert "/api/v1/commander/commands" in paths
    assert "/api/v1/collector/config/prepare" in paths
    assert "/api/v1/devices/{device_id}/trend" not in paths


def test_openapi_generator_is_deterministic() -> None:
    assert render() == render()
