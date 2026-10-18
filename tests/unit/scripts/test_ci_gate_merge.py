"""merge_gate 受影响范围选择的确定性测试。

通过接管 _run_many 捕获计划执行的检查，不实际运行 pytest/npm。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "scripts" / "ci_gate.py"


def _load_gate(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[tuple[str, list[str]]]]:
    spec = importlib.util.spec_from_file_location("wind_hub_ci_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: list[tuple[str, list[str]]] = []

    def _fake_run_many(
        commands: list[tuple[str, list[str]]],
    ) -> bool:
        captured.extend(commands)
        return True

    monkeypatch.setattr(module, "_run_many", _fake_run_many)
    return module, captured


def _selected_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *changed: str
) -> tuple[list[str], list[str], list[list[str]]]:
    module, captured = _load_gate(monkeypatch)
    assert module.merge_gate(list(changed))
    summary = json.loads(capsys.readouterr().out)
    return summary["selected_tests"], [label for label, _ in captured], [
        command for _, command in captured
    ]


def test_markdown_only_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected, labels, _ = _selected_paths(
        monkeypatch, capsys, "ai_shared/rules/routing.md", "README.md"
    )
    assert selected == []
    assert labels == []


def test_ads_driver_selects_protocol_and_recovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected, labels, commands = _selected_paths(
        monkeypatch, capsys, "src/core/infrastructure/protocol/ads/driver.py"
    )
    assert {
        "tests/unit/core/protocol/ads",
        "tests/component/core/protocol/ads",
        "tests/integration/protocols/ads",
        "tests/reliability/recovery",
    }.issubset(set(selected))
    assert "tests/unit" not in selected
    assert {"ruff", "mypy", "import-linter", "affected-tests"}.issubset(set(labels))
    assert any("-m" in command for command in commands)


def test_modbus_driver_has_no_recovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected, _, _ = _selected_paths(
        monkeypatch, capsys, "src/core/infrastructure/protocol/modbus/driver.py"
    )
    assert {
        "tests/unit/core/protocol/modbus",
        "tests/component/core/protocol/modbus",
        "tests/integration/protocols/modbus",
    }.issubset(set(selected))
    assert "tests/reliability/recovery" not in selected


def test_sink_uses_real_locations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected, _, _ = _selected_paths(
        monkeypatch, capsys, "src/core/infrastructure/sink/redis.py"
    )
    assert "tests/integration/sinks" in selected
    assert "tests/unit/collector/sink" in selected
    assert "tests/unit/core/test_sink_contract.py" in selected
    assert "tests/component/collector/test_modbus_sink_server.py" in selected
    # 旧的不存在目录不得再出现在选择结果中。
    assert "tests/unit/core/sink" not in selected
    assert "tests/component/core/sink" not in selected


def test_web_server_has_no_phantom_component_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected, _, _ = _selected_paths(
        monkeypatch, capsys, "src/web_server/application/app.py"
    )
    assert selected == ["tests/unit/web_server"]


def test_grpc_does_not_shadow_subsystem_tests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    collector, _, _ = _selected_paths(
        monkeypatch, capsys, "src/collector/infrastructure/grpc/servicer.py"
    )
    assert "tests/contract/rpc" in collector
    assert "tests/component/collector" in collector
    assert "tests/unit/collector" in collector

    commander, _, _ = _selected_paths(
        monkeypatch, capsys, "src/commander/infrastructure/grpc/client.py"
    )
    assert {
        "tests/contract/rpc",
        "tests/unit/commander",
        "tests/component/commander",
    }.issubset(set(commander))

    web_server, _, _ = _selected_paths(
        monkeypatch, capsys, "src/web_server/adapter/outbound/grpc/client.py"
    )
    assert "tests/contract/rpc" in web_server
    assert "tests/unit/web_server" in web_server


def test_collector_startup_selects_system_acquisition(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assembly, _, _ = _selected_paths(
        monkeypatch, capsys, "src/collector/assembly.py", "src/collector/main.py"
    )
    assert "tests/system/acquisition" in assembly


def test_commander_application_selects_system_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected, _, _ = _selected_paths(
        monkeypatch, capsys, "src/commander/application/dispatcher.py"
    )
    assert {
        "tests/unit/commander",
        "tests/component/commander",
        "tests/system/command",
    }.issubset(set(selected))


def test_point_table_selects_config_layers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected, _, _ = _selected_paths(
        monkeypatch, capsys, "src/core/infrastructure/config/point_table_writer.py"
    )
    assert set(selected) == {"tests/unit/core/config", "tests/contract"}


def test_dependency_change_runs_broad_backend_tests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected, labels, _ = _selected_paths(
        monkeypatch, capsys, "pyproject.toml"
    )
    assert {"tests/unit", "tests/component", "tests/contract"}.issubset(set(selected))
    assert "mypy" in labels


def test_frontend_shell_triggers_fast_and_e2e(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, labels, _ = _selected_paths(
        monkeypatch, capsys, "src/web_admin/src/App.vue"
    )
    assert {"frontend-build", "frontend-unit", "frontend-e2e"}.issubset(set(labels))


def test_playwright_config_is_e2e_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, labels, _ = _selected_paths(
        monkeypatch, capsys, "src/web_admin/playwright.config.ts"
    )
    assert "frontend-e2e" in labels
    assert "frontend-build" not in labels
    assert "frontend-unit" not in labels


def test_frontend_package_change_triggers_e2e(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, labels, _ = _selected_paths(
        monkeypatch, capsys, "src/web_admin/package.json"
    )
    assert {"frontend-build", "frontend-unit", "frontend-e2e"}.issubset(set(labels))


def test_frontend_e2e_spec_does_not_trigger_backend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected, labels, _ = _selected_paths(
        monkeypatch, capsys, "tests/web_admin/e2e/shell.spec.ts"
    )
    assert selected == []
    assert labels == ["frontend-e2e"]


def test_tooling_change_runs_compileall(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, labels, _ = _selected_paths(
        monkeypatch, capsys, "scripts/ci_gate.py"
    )
    assert "tooling-syntax" in labels


def test_changed_test_file_runs_itself(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected, _, _ = _selected_paths(
        monkeypatch,
        capsys,
        "tests/unit/scripts/test_ci_scope.py",
    )
    assert selected == ["tests/unit/scripts/test_ci_scope.py"]


def test_missing_candidate_path_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module, _ = _load_gate(monkeypatch)
    monkeypatch.setattr(module, "REPO_ROOT", ROOT / "no-such-repo-root")
    assert not module.merge_gate(["src/core/domain/point.py"])
    err = capsys.readouterr().err
    assert "selected test paths do not exist" in err
