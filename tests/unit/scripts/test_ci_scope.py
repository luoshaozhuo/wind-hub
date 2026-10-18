"""Scope mapping guards for refactored production directories."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCOPE = ROOT / "scripts" / "ci_scope.py"


def _classify(path: str) -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("wind_hub_ci_scope", SCOPE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.classify([path])


def test_ads_protocol_selects_integration() -> None:
    result = _classify("src/core/infrastructure/protocol/ads/driver.py")
    assert "integration-protocol" in result["backend_pr_targets"]


def test_sink_core_layout_selects_integration() -> None:
    result = _classify("src/core/infrastructure/sink/file.py")
    assert "integration-sinks" in result["backend_pr_targets"]


def test_governance_does_not_trigger_product_tests() -> None:
    result = _classify("ai_shared/rules/routing.md")
    assert result["product"] == "false"
    assert result["backend_fast_required"] == "false"


def test_small_backend_change_does_not_select_all_fast_layers() -> None:
    result = _classify("src/core/domain/point.py")
    assert result["backend_fast_targets"] != "unit,component,contract"
