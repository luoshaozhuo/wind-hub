"""Core 测试按业务能力组织；层级通过 Marker 明确标识。"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """非 Adapter 专项测试默认 unit；协议六类专项由 protocol/conftest 决定。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent
    protocol = root / "protocol"
    for item in items:
        if not item.path.is_relative_to(root):
            continue
        if item.path.is_relative_to(protocol) and item.path.stem in {
            "test_contract", "test_functional", "test_integration",
            "test_fault_injection", "test_concurrency", "test_stability",
            "test_config_contract",
        }:
            continue
        if item.path.name in {"test_protocol_registry.py", "test_protocol_sample_contract.py"}:
            item.add_marker(pytest.mark.contract)
        else:
            item.add_marker(pytest.mark.unit)
