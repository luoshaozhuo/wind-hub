"""ci_scope.py 变更路径 → gate 映射的回归测试。
固定关键架构路径的 PR Gate 触发关系：collector sink/query 应用服务必须
触发 RPC 与 sink 相关集成测试；collector/commander 配置与装配路径必须
触发对应 system 套件。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_ci_scope() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_scope", REPO_ROOT / "scripts" / "ci_scope.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pr_targets(*paths: str) -> set[str]:
    result = _load_ci_scope().classify(list(paths))
    return set(filter(None, result["backend_pr_targets"].split(",")))


def test_collector_configuration_triggers_system_acquisition() -> None:
    assert "system-acquisition" in _pr_targets("src/collector/application/configuration.py")


def test_commander_config_triggers_system_command() -> None:
    assert "system-command" in _pr_targets("src/commander/application/config_apply.py")


def test_collector_publisher_triggers_sink_gate() -> None:
    targets = _pr_targets("src/collector/infrastructure/adapters/outbound/publisher.py")
    assert "integration-sinks" in targets


def test_collector_grpc_service_triggers_rpc_gate() -> None:
    assert "integration-rpc" in _pr_targets(
        "src/collector/infrastructure/adapters/inbound/grpc/collector_service.py"
    )


def test_playwright_spec_changes_trigger_frontend_e2e() -> None:
    result = _load_ci_scope().classify(["tests/web_admin/e2e/shell.spec.ts"])
    assert result["frontend_e2e"] == "true"
    assert result["frontend_fast"] == "false"


def test_vitest_spec_changes_trigger_frontend_fast() -> None:
    result = _load_ci_scope().classify(["tests/web_admin/unit/status.spec.ts"])
    assert result["frontend_fast"] == "true"
    assert result["frontend_e2e"] == "false"


def test_pr_targets_stay_consumable_by_ci_gate() -> None:
    """ci_scope 输出的 PR target 必须全部存在于 ci_gate.PR_TARGETS。"""
    import importlib.util as _util

    spec = _util.spec_from_file_location("ci_gate", REPO_ROOT / "scripts" / "ci_gate.py")
    assert spec is not None and spec.loader is not None
    ci_gate = _util.module_from_spec(spec)
    spec.loader.exec_module(ci_gate)

    scope = _load_ci_scope()
    unknown = set(scope.PR_TARGET_ORDER) - set(ci_gate.PR_TARGETS)
    assert unknown == set()


def test_data_generator_is_known_backend_source() -> None:
    result = _load_ci_scope().classify(["src/data_generator/application/runtime.py"])
    assert result["backend_static"] == "true"
    assert result["backend_fast_targets"] == "unit"


def test_data_generator_changes_trigger_own_integration() -> None:
    assert "integration-generator" in _pr_targets("src/data_generator/application/runtime.py")


def test_gateway_changes_trigger_own_integration() -> None:
    assert "integration-gateway" in _pr_targets(
        "src/industrial_gateway/application/variable_leases.py"
    )


def test_sink_changes_trigger_sink_integration() -> None:
    assert "integration-sinks" in _pr_targets("src/sink_service/application/reliable_engine.py")


@pytest.mark.parametrize(
    "path",
    [
        "src/storage_service/main.py",
        "tests/storage_service/integration/test_kafka_real.py",
        "src/contracts/storage_service.proto",
        "src/contracts/configuration_repository.proto",
        "src/core/configuration/deployment.py",
        "configs/services/service_storage-main_v1.1.0.yaml",
        "configs/services/storage_bootstrap_v1.0.0.yaml",
        "tests/configuration_service/integration/test_storage_deployment_flow.py",
    ],
)
def test_storage_changes_trigger_storage_integration(path: str) -> None:
    assert "integration-storage" in _pr_targets(path)
