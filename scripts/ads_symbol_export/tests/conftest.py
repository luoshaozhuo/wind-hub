"""独立测试配置及 ADS mock 隔离。"""

from __future__ import annotations

import ctypes
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ads_export import connection, file_service, runtime
from ads_export.config import Settings, load_config

@pytest.fixture
def settings() -> Settings:
    result = load_config(Path(__file__).resolve().parents[1] / "ads_symbol_export_v1.0.0.yaml")
    result.TRY_FULL_TABLE_FIRST = False
    return result


@pytest.fixture
def fake_ads(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("pyads")
    module.PLCTYPE_BYTE = ctypes.c_ubyte
    module.Connection = MagicMock()
    module.add_route = MagicMock()
    module.add_route_to_plc = MagicMock()
    monkeypatch.setitem(sys.modules, "pyads", module)
    for target in (connection, runtime, file_service):
        monkeypatch.setattr(target, "pyads", module)
    return module

@pytest.fixture(autouse=True)
def restore_runtime_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore direct test replacements of runtime functions."""
    for name in ("_read_bytes", "_export_full_table", "_export_in_chunks"):
        monkeypatch.setattr(runtime, name, getattr(runtime, name))
