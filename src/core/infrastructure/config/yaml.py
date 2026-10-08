"""共享 YAML 配置读取适配器；不依赖 Collector 或 Commander。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import yaml

from core.application import ConfigError


class YamlConfigReader:
    """一次构造确定配置根目录；按主题分别读取，调用方自行组合。"""

    def __init__(self, config_dir: str | Path) -> None:
        self._base = Path(config_dir)

    def read_system(self) -> dict[str, Any]:
        return self._read("system.yaml")

    def read_devices(self) -> dict[str, Any]:
        return self._read("devices.yaml")

    def read_device_models(self) -> dict[str, Any]:
        return self._read("device_models.yaml")

    def read_points(self) -> dict[str, Any]:
        return self._read("points.yaml")

    def read_units(self) -> dict[str, Any]:
        return self._read("units.yaml")

    def read_tasks(self) -> dict[str, Any]:
        return self._read("tasks.yaml")

    def read_sinks(self) -> dict[str, Any]:
        return self._read("sinks.yaml")

    def _read(self, filename: str) -> dict[str, Any]:
        return read_yaml_mapping(self._base / filename)

    def fingerprint(self) -> str:
        return fingerprint_config_set(self._base)


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


def fingerprint_config_set(config_dir: str | Path) -> str:
    """现场 YAML 配置集稳定 SHA-256，保持原有跨进程算法。"""
    site_dir = Path(config_dir).resolve()
    digest = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    for path in site_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        relative_path = path.relative_to(site_dir)
        if ".history" in relative_path.parts:
            continue
        files.append((relative_path.as_posix(), path))

    for relative, path in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
