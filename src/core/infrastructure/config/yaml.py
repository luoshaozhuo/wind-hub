"""共享 YAML 配置读取适配器；不依赖 Collector 或 Commander。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from core.application import ConfigError
from core.domain.config import ConfigTopic

_TOPIC_FILES: dict[ConfigTopic, str] = {
    ConfigTopic.SYSTEM: "system.yaml",
    ConfigTopic.DEVICE_MODELS: "device_models.yaml",
    ConfigTopic.DEVICES: "devices.yaml",
    ConfigTopic.POINTS: "points.yaml",
    ConfigTopic.UNITS: "units.yaml",
    ConfigTopic.TASKS: "tasks.yaml",
    ConfigTopic.SINKS: "sinks.yaml",
}


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


def fingerprint_config_topics(
    config_dir: str | Path,
    topics: Iterable[ConfigTopic],
) -> str:
    """指定配置主题文件的稳定指纹（主题作用域的版本一致性检查）。"""
    return fingerprint_config_files(config_dir, [_TOPIC_FILES[topic] for topic in topics])


def fingerprint_config_files(config_dir: str | Path, filenames: Iterable[str]) -> str:
    """指定文件子集的稳定 SHA-256；与目录指纹使用相同的混合算法。

    缺失的主题文件按空内容计入，使"文件被删除"同样表现为指纹变化。
    """
    site_dir = Path(config_dir).resolve()
    digest = hashlib.sha256()
    for name in sorted(filenames):
        path = site_dir / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
