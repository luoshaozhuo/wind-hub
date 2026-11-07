"""Core 通用单文件 YAML 处理；项目模板装配由 configuration 拥有。"""

from .generic_yaml import (
    MISSING,
    ConfigDiff,
    ConfigDiffEntry,
    ConfigErrorCode,
    ConfigOperationError,
    compare_config,
    dump_yaml,
    parse_yaml,
    read_yaml,
    save_yaml,
)

__all__ = [
    "ConfigDiff",
    "ConfigDiffEntry",
    "ConfigErrorCode",
    "ConfigOperationError",
    "MISSING",
    "compare_config",
    "dump_yaml",
    "parse_yaml",
    "read_yaml",
    "save_yaml",
]
