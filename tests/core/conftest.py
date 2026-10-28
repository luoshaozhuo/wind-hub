"""Core 测试按业务能力组织；层级通过 Marker 明确标识。"""

from __future__ import annotations

from pathlib import Path

import pytest

_LEVEL_MARKERS = {
    "unit",
    "component",
    "contract",
    "integration",
    "system",
    "reliability",
    "performance",
}

# 协议六类专项由 protocol/conftest 按文件名决定层级。
_PROTOCOL_SUITES = {
    "test_contract",
    "test_functional",
    "test_integration",
    "test_fault_injection",
    "test_concurrency",
    "test_stability",
    "test_config_contract",
}
# 协议目录之外的两个纯契约文件。
_CONTRACT_FILES = {"test_protocol_registry.py", "test_protocol_sample_contract.py"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """未显式声明层级的 Core 测试：协议专项跳过，契约文件标 contract，其余 unit。"""
    root = Path(__file__).resolve().parent
    protocol = root / "protocol"
    for item in items:
        if not item.path.is_relative_to(root):
            continue
        existing = {marker.name for marker in item.iter_markers()}
        if _LEVEL_MARKERS & existing:
            continue
        if item.path.is_relative_to(protocol) and item.path.stem in _PROTOCOL_SUITES:
            continue
        item.add_marker(
            pytest.mark.contract if item.path.name in _CONTRACT_FILES else pytest.mark.unit
        )
