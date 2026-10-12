"""共享 YAML 配置读取适配器；不依赖 Collector 或 Commander。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import SecretStr

from core.application import ConfigError


class _UniqueKeyLoader(yaml.SafeLoader):
    """拒绝重复 Mapping Key 的 YAML Loader（PyYAML 默认静默覆盖）。"""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise ConfigError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def read_yaml_mapping(path: str | Path) -> dict[str, Any]:
    """安全读取 YAML 根映射，拒绝重复 Key，保留既有配置异常语义。"""
    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"Configuration file not found: {source}")
    try:
        with source.open(encoding="utf-8") as handle:
            data = cast(Any, yaml.load(handle, Loader=_UniqueKeyLoader))
    except ConfigError as exc:
        raise ConfigError(f"Invalid YAML in {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {source}: {exc}") from exc
    if data is None:
        raise ConfigError(f"Empty configuration file: {source}")
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root must be a mapping: {source}")
    return data


def yaml_plain(value: Any) -> Any:
    """把带类型的配置值（SecretStr/Enum/嵌套容器）还原为纯 YAML 标量结构。"""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: yaml_plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [yaml_plain(item) for item in value]
    return value


def write_yaml_mapping_atomic(path: str | Path, data: Mapping[str, Any]) -> None:
    """原子写入 YAML 映射：同目录临时文件 → 完整写入 → fsync → 原子替换。

    失败时清理临时文件，目标文件保持原样，不会处于部分写入状态。
    """
    target = Path(path)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            yaml.safe_dump(dict(data), handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
