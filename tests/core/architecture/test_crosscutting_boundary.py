"""Core v1_21 横切/主动诊断/RPC 边界的静态回归测试。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.safety]

_ROOT = Path(__file__).resolve().parents[3]
_INFRA = _ROOT / "src" / "core" / "infrastructure"
_SRC = _ROOT / "src"

_CROSSCUTTING = {
    "__init__.py",
    "async_log.py",
    "client_correlation.py",
    "correlation.py",
    "grpc_correlation.py",
    "log_setup.py",
    "observability.py",
    "observed_protocol.py",
    "tracing.py",
}
_ACTIVE_DIAGNOSTIC = {
    "__init__.py",
    "benchmark.py",
    "firewall.py",
    "network.py",
    "protocol.py",
}
_OLD_IMPORTS = {
    f"core.infrastructure.diagnostic.{name}"
    for name in (
        "async_log",
        "correlation",
        "log_setup",
        "observability",
        "observed_protocol",
        "tracing",
    )
} | {
    "core.infrastructure.rpc.correlation",
    "core.infrastructure.rpc.client_correlation",
}
_OLD_DIAGNOSTIC_EXPORTS = {
    "CorrelationSettings",
    "JsonFormatter",
    "LogFileSettings",
    "LoggingSettings",
    "MetricsSettings",
    "ObservabilityHandle",
    "ObservabilitySettings",
    "RequestIdASGIMiddleware",
    "TracingSettings",
    "configure_observability",
    "parse_observability_settings",
}


def test_infrastructure_directories_respect_separation() -> None:
    crosscutting = {path.name for path in (_INFRA / "crosscutting").glob("*.py")}
    diagnostic = {path.name for path in (_INFRA / "diagnostic").glob("*.py")}
    rpc = {path.name for path in (_INFRA / "rpc").glob("*.py")}
    assert _CROSSCUTTING <= crosscutting
    assert diagnostic == _ACTIVE_DIAGNOSTIC
    assert rpc == {"__init__.py", "scalar_codec.py"}


def test_no_production_imports_of_obsolete_crosscutting_paths() -> None:
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
                names.extend(f"{node.module}.{alias.name}" for alias in node.names)
                if node.module == "core.infrastructure.diagnostic" and any(
                    alias.name in _OLD_DIAGNOSTIC_EXPORTS for alias in node.names
                ):
                    offenders.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
            if any(
                name == old or name.startswith(old + ".")
                for name in names
                for old in _OLD_IMPORTS
            ):
                offenders.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    assert not offenders, "obsolete Core imports: " + ", ".join(sorted(set(offenders)))
