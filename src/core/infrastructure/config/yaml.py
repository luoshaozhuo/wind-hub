"""共享 YAML 配置读取适配器；不依赖 Collector 或 Commander。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from core.application import ConfigError

def read_yaml_mapping(path: str | Path) -> dict[str, Any]:
    """安全读取 YAML 根映射，保留既有配置异常语义。"""
    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"Configuration file not found: {source}")
    try:
        with source.open(encoding="utf-8") as handle:
            data = cast(Any, yaml.safe_load(handle))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {source}: {exc}") from exc
    if data is None:
        raise ConfigError(f"Empty configuration file: {source}")
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root must be a mapping: {source}")
    return data


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


def active_config_dir(root: str | Path) -> Path:
    """Resolve one consistent published YAML generation from a config root."""
    base = Path(root)
    pointer = base / ".active-config"
    if not pointer.exists():
        return base
    generation = pointer.read_text(encoding="ascii").strip()
    if len(generation) != 32 or any(letter not in "0123456789abcdef" for letter in generation):
        raise ConfigError("Invalid active configuration generation")
    version = base / ".config-versions" / generation
    if not version.is_dir():
        raise ConfigError(f"Active configuration version is missing: {generation}")
    return version
