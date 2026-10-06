"""``wind_hub_core.config`` public façade 与内部结构约束。

- façade ``__all__`` 显式覆盖全部生产 public model 与入口函数，且每个名字
  真实可导入；
- 旧物理模块（schema / sinks / device_resolver / point_table_resolver /
  sink_resolver）不复存在；
- 生产代码不直接 import config 内部物理模块（model / resolver /
  validation / loader / diff / fingerprint），统一走 façade；
- model / resolver / validation / loader 之间无循环 import（分层约束由
  import-linter 固化，此处验证包可干净导入）。
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import wind_hub_core.config as config_facade

SRC_ROOT = Path(__file__).resolve().parents[4] / "src"
CONFIG_PACKAGE = SRC_ROOT / "wind_hub_core" / "config"

EXPECTED_FACADE_EXPORTS = frozenset(
    {
        # system.yaml
        "ADSSystemConfig",
        "SiteConfig",
        "RuntimeConfig",
        "ApiConfig",
        "InterfaceConfig",
        "SystemConfig",
        # units.yaml
        "UnitConfig",
        "UnitsConfig",
        # device domain
        "SUPPORTED_PROTOCOLS",
        "DeviceTypeConfig",
        "DeviceModelConfig",
        "DeviceModelsConfig",
        "InstanceEndpoint",
        "DeviceInstanceConfig",
        "DeviceInstancesConfig",
        "DeviceConfig",
        # point domain
        "PointAddress",
        "PointConfig",
        "PointPatch",
        "PointTableConfig",
        "PointTablesConfig",
        "ResolvedPointTable",
        # task domain
        "TaskTarget",
        "CollectionTaskConfig",
        "TasksConfig",
        # sink domain
        "SINK_TYPES",
        "SINK_DATA_TYPES",
        "SINK_NUMERIC_DATA_TYPES",
        "MODBUS_WORD_WIDTH",
        "SinkSource",
        "FileSinkConnection",
        "KafkaSinkConnection",
        "DatabaseSinkConnection",
        "IEC104SinkConnection",
        "OPCUASinkConnection",
        "ModbusSinkConnection",
        "StreamSinkAddress",
        "IEC104SinkAddress",
        "OPCUASinkAddress",
        "ModbusSinkAddress",
        "SinkPoint",
        "ResolvedSinkPoint",
        "SinkConfig",
        "ResolvedSinkConfig",
        "SinksConfig",
        # 顶层聚合
        "Config",
        # 解析 / 加载 / 差异 / 指纹入口
        "resolve_devices",
        "resolve_point_tables",
        "resolve_sinks",
        "load_config",
        "fingerprint_config_set",
        "compute_diff",
    }
)

REMOVED_MODULES = (
    "schema.py",
    "sinks.py",
    "device_resolver.py",
    "point_table_resolver.py",
    "sink_resolver.py",
)

# 生产代码禁止直接 import 的 config 内部模块（import-linter 之外的源码级防线，
# 覆盖 import-linter root_packages 之外的脚本与测试支撑代码）。
_INTERNAL_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+wind_hub_core\.config\."
    r"(?:model|resolver|validation|loader|diff|fingerprint|schema|sinks)\b",
    re.MULTILINE,
)


def test_facade_exports_exact_public_set() -> None:
    """façade ``__all__`` 与预期 public 集合严格一致（不允许隐式增删）。"""
    assert set(config_facade.__all__) == EXPECTED_FACADE_EXPORTS
    assert len(config_facade.__all__) == len(EXPECTED_FACADE_EXPORTS)


def test_facade_exports_are_importable() -> None:
    """``__all__`` 中每个名字都是 façade 的真实属性。"""
    missing = [name for name in config_facade.__all__ if not hasattr(config_facade, name)]
    assert missing == []


def test_removed_physical_modules_do_not_exist() -> None:
    """旧物理模块已删除，无 compatibility re-export 层。"""
    survivors = [name for name in REMOVED_MODULES if (CONFIG_PACKAGE / name).is_file()]
    assert survivors == []


def test_internal_layers_import_cleanly() -> None:
    """model / resolver / validation / loader 分层可干净导入（无循环）。"""
    for module in (
        "wind_hub_core.config.model.config",
        "wind_hub_core.config.resolver.device",
        "wind_hub_core.config.resolver.point_table",
        "wind_hub_core.config.resolver.sink",
        "wind_hub_core.config.validation.references",
        "wind_hub_core.config.validation.addresses",
        "wind_hub_core.config.validation.task",
        "wind_hub_core.config.loader",
    ):
        importlib.import_module(module)


def _production_source_files() -> list[Path]:
    return [
        path
        for path in SRC_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and CONFIG_PACKAGE not in path.parents
    ]


def test_production_code_uses_facade_only() -> None:
    """config 包外的生产代码不直接 import config 内部物理模块。"""
    offenders = [
        path
        for path in _production_source_files()
        if _INTERNAL_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
