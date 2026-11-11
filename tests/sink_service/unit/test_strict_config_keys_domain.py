"""R13（v1_47）domain 层严格配置校验。

覆盖设计 v1_47 §11/§14 的未知字段拒绝与 caller 准入规则，直接测试
``domain.config``（不经过 application 快照装配），使严格键集契约独立于
快照加载链路被钉死：

- ``require_keys`` 同时拒绝缺失必填键与未知键，可选键显式放行；
- ``validate_service`` 对各嵌套节的未知键、非法 caller 权限/重复身份、
  bounded ``max_deliver`` 一律 fail-closed；
- 随仓夹具的基准 Service 文档必须通过（防止严格校验误拒合法配置）。
"""

from __future__ import annotations

import copy

import pytest

from sink_service.domain.config import (
    ConfigurationError,
    require_keys,
    validate_service,
)
from tests.sink_service._stage1_fixtures import service_document

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def test_require_keys_accepts_exact_and_optional() -> None:
    require_keys({"a": 1, "b": 2}, "doc", frozenset({"a", "b"}))
    require_keys({"a": 1, "b": 2, "c": 3}, "doc", frozenset({"a", "b"}), frozenset({"c"}))


def test_require_keys_rejects_unknown_and_missing() -> None:
    with pytest.raises(ConfigurationError, match="unknown"):
        require_keys({"a": 1, "x": 0}, "doc", frozenset({"a"}))
    with pytest.raises(ConfigurationError, match="missing"):
        require_keys({"a": 1}, "doc", frozenset({"a", "b"}))


def test_reference_service_document_passes_strict_validation() -> None:
    validate_service(service_document())


def test_unknown_top_level_service_key_rejected() -> None:
    doc = service_document(overrides={"unexpected_field": 1})
    with pytest.raises(ConfigurationError, match="unknown"):
        validate_service(doc)


@pytest.mark.parametrize(
    ("section", "payload"),
    [
        (
            "grpc",
            {
                "listen_host": "127.0.0.1",
                "listen_port": 50061,
                "max_active_rpcs": 64,
                "max_request_bytes": 2 * 1024**2,
                "default_deadline_ms": 5000,
                "extra": 1,
            },
        ),
        (
            "runtime",
            {
                "max_inflight_global": 128,
                "max_inflight_per_sink": 8,
                "max_pending_points": 10000,
                "io_wait_timeout_ms": 3000,
                "max_event_records": 10000,
                "extra": 1,
            },
        ),
        (
            "metrics",
            {
                "metrics_compute_interval_ms": 1000,
                "metrics_window_ms": 60000,
                "supported_window_ms": [60000],
                "max_time_buckets": 120,
                "overrun_policy": "skip",
                "extra": 1,
            },
        ),
        ("shutdown", {"grace_seconds": 10, "extra": 1}),
    ],
)
def test_unknown_nested_section_key_rejected(section: str, payload: dict) -> None:
    doc = service_document()
    doc[section] = payload
    with pytest.raises(ConfigurationError, match="unknown"):
        validate_service(doc)


def test_legacy_dual_process_fields_still_rejected() -> None:
    doc = service_document(overrides={"process_role": "reliable"})
    with pytest.raises(ConfigurationError):
        validate_service(doc)


def test_bounded_max_deliver_rejected() -> None:
    doc = service_document()
    doc["reliable"]["max_deliver"] = 5
    with pytest.raises(ConfigurationError, match="max_deliver"):
        validate_service(doc)


def test_callers_must_be_non_empty_list() -> None:
    doc = service_document(callers=[])
    with pytest.raises(ConfigurationError, match="callers"):
        validate_service(doc)


def test_caller_unknown_permission_rejected() -> None:
    doc = service_document(
        callers=[
            {
                "caller_id": "maintenance",
                "certificate_identity": "spiffe://wind-hub/maintenance",
                "permissions": ["query", "root"],
            }
        ]
    )
    with pytest.raises(ConfigurationError, match="permission"):
        validate_service(doc)


def test_caller_duplicate_permission_rejected() -> None:
    doc = service_document(
        callers=[
            {
                "caller_id": "maintenance",
                "certificate_identity": "spiffe://wind-hub/maintenance",
                "permissions": ["query", "query"],
            }
        ]
    )
    with pytest.raises(ConfigurationError, match="permission"):
        validate_service(doc)


def test_duplicate_caller_id_and_identity_rejected() -> None:
    base = {
        "caller_id": "maintenance",
        "certificate_identity": "spiffe://wind-hub/maintenance",
        "permissions": ["query"],
    }
    second = copy.deepcopy(base)
    second["certificate_identity"] = "spiffe://wind-hub/other"
    doc = service_document(callers=[base, second])
    with pytest.raises(ConfigurationError, match="duplicat"):
        validate_service(doc)

    identity_twin = copy.deepcopy(base)
    identity_twin["caller_id"] = "other"
    doc2 = service_document(callers=[copy.deepcopy(base), identity_twin])
    with pytest.raises(ConfigurationError, match="duplicat"):
        validate_service(doc2)


def test_caller_unknown_key_rejected() -> None:
    doc = service_document(
        callers=[
            {
                "caller_id": "maintenance",
                "certificate_identity": "spiffe://wind-hub/maintenance",
                "permissions": ["query"],
                "self_declared_role": "admin",
            }
        ]
    )
    with pytest.raises(ConfigurationError, match="unknown"):
        validate_service(doc)
