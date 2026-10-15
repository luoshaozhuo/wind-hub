"""Web Admin HTTP API 的 OpenAPI 契约测试。

``src/web_admin/src/api/generated/openapi.json`` 是前端契约类型
（schema.d.ts）的唯一来源。后端路由/DTO 变更而未重新生成契约时，
前端会在运行时静默漂移——本测试直接比对实时 ``build_api().openapi()``
与已提交产物，强制契约变更显式化。

漂移修复方式：``python3 scripts/generate_api_contract.py``。
"""

from __future__ import annotations

import json
from pathlib import Path

from web_server.adapter.inbound.webapi.app import build_api
from web_server.application.app_context import AppContext

GENERATED = (
    Path(__file__).parents[3] / "src" / "web_admin" / "src" / "api" / "generated" / "openapi.json"
)

# 关键资源与方法：防止路由被意外删除或改方法。
_CRITICAL_OPERATIONS = {
    ("GET", "/api/v1/devices"),
    ("GET", "/api/v1/devices/{device_id}"),
    ("POST", "/api/v1/devices/{device_id}/commands"),
    ("GET", "/api/v1/tasks"),
    ("POST", "/api/v1/tasks/{task_id}/start"),
    ("POST", "/api/v1/tasks/{task_id}/stop"),
    ("GET", "/api/v1/sinks"),
    ("POST", "/api/v1/sinks/{name}/verify"),
    ("POST", "/api/v1/config/apply"),
    ("PUT", "/api/v1/admin-state"),
    ("GET", "/api/v1/operations/{operation_id}"),
    ("POST", "/api/v1/diagnostics/protocol/write"),
}


def _live_schema() -> dict:
    return build_api(AppContext()).openapi()


def test_committed_openapi_matches_live_schema() -> None:
    committed = json.loads(GENERATED.read_text(encoding="utf-8"))
    live = _live_schema()

    assert committed == live, (
        "committed openapi.json drifted from the live FastAPI schema; "
        "regenerate with: python3 scripts/generate_api_contract.py"
    )


def test_critical_operations_are_present() -> None:
    paths = _live_schema().get("paths", {})
    missing = [
        f"{method} {path}"
        for method, path in sorted(_CRITICAL_OPERATIONS)
        if method.lower() not in paths.get(path, {})
    ]

    assert missing == []


def test_error_envelope_is_documented_for_validation_failures() -> None:
    """统一错误包络模型必须出现在组件 schema 中（前端按 code 分支）。"""
    components = _live_schema().get("components", {}).get("schemas", {})

    assert "HTTPValidationError" in components
