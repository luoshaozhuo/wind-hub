"""ci_scope.py 变更路径 → gate 映射的回归测试。

固定关键架构路径的 PR Gate 触发关系：collector sink/query 应用服务必须
触发 RPC 与 sink 相关集成测试；collector/commander 配置与装配路径必须
触发对应 system 套件。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

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


def test_collector_config_service_triggers_system_acquisition() -> None:
    assert "system-acquisition" in _pr_targets("src/collector/application/config_service.py")


def test_commander_config_triggers_system_command() -> None:
    assert "system-command" in _pr_targets("src/commander/application/config.py")


def test_collector_sink_service_triggers_rpc_and_sink_gates() -> None:
    targets = _pr_targets("src/collector/application/sink_runtime.py")
    assert "integration-rpc" in targets
    assert "integration-sinks" in targets


def test_collector_query_service_triggers_rpc_gate() -> None:
    assert "integration-rpc" in _pr_targets("src/collector/application/services.py")
