"""ci_scope.py 变更路径 → gate 映射的回归测试（Phase 6）。

固定关键架构路径的 PR Gate 触发关系：placement reconciler 等 task control
核心路径必须触发 system-task-control；collector sink/query 应用服务必须
触发 RPC 与 sink 相关集成测试；commander 配置事务服务必须触发 system-reload。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_ci_scope() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ci_scope", REPO_ROOT / "scripts" / "ci_scope.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pr_targets(*paths: str) -> set[str]:
    result = _load_ci_scope().classify(list(paths))
    return set(filter(None, result["backend_pr_targets"].split(",")))


def test_reconciler_triggers_system_task_control() -> None:
    assert "system-task-control" in _pr_targets(
        "src/wind_hub_server/application/task/reconcile.py"
    )


def test_task_control_plane_paths_trigger_system_task_control() -> None:
    for path in (
        "src/wind_hub_server/application/task/placement.py",
        "src/wind_hub_server/application/task/control.py",
        "src/wind_hub_server/application/task/collector.py",
        "src/wind_hub_server/application/worker/registry.py",
        "src/wind_hub_server/application/worker/model.py",
    ):
        assert "system-task-control" in _pr_targets(path), path


def test_commander_config_service_triggers_system_reload() -> None:
    assert "system-reload" in _pr_targets(
        "src/wind_hub_commander/application/config.py"
    )


def test_collector_sink_service_triggers_rpc_and_sink_gates() -> None:
    targets = _pr_targets("src/wind_hub_collector/application/service/sink.py")
    assert "integration-rpc" in targets
    assert "integration-sinks" in targets


def test_collector_query_service_triggers_rpc_gate() -> None:
    assert "integration-rpc" in _pr_targets(
        "src/wind_hub_collector/application/service/query.py"
    )
