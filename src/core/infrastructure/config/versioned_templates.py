"""版本化模板只读解析：路径安全、精确引用与内容摘要。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.application.errors import ConfigError

from .yaml import read_yaml_mapping

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_CATEGORIES = frozenset({"protocols", "device_models", "point_tables", "tasks", "deliveries"})


def _identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ConfigError(f"invalid {label}: {value!r}")
    return value


def _version(value: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ConfigError(f"invalid template version: {value!r}")
    return value


def config_hash(document: Mapping[str, Any]) -> str:
    """基于 YAML 解析后的规范化内容计算 SHA-256，独立于键顺序。"""
    try:
        canonical = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"configuration cannot be hashed: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TemplateRef:
    category: str
    template_id: str
    template_version: str

    def __post_init__(self) -> None:
        if self.category not in _CATEGORIES:
            raise ConfigError(f"unsupported template category: {self.category!r}")
        _identifier(self.template_id, label="template ID")
        _version(self.template_version)


@dataclass(frozen=True, slots=True)
class ResolvedTemplate:
    reference: TemplateRef
    path: Path
    content_hash: str
    document: Mapping[str, Any]


class VersionedTemplateRepository:
    """仅访问预定义类别下的精确版本文件，绝不回退到最新版。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    def path_for(self, reference: TemplateRef) -> Path:
        candidate = (
            self._root
            / reference.category
            / reference.template_id
            / f"{reference.template_version}.yaml"
        ).resolve()
        if not candidate.is_relative_to(self._root):
            raise ConfigError("template reference escapes configured root")
        return candidate

    def resolve(self, reference: TemplateRef) -> ResolvedTemplate:
        path = self.path_for(reference)
        document = read_yaml_mapping(path)
        if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
            raise ConfigError(f"{path}: unsupported schema_version")
        if document.get("template_id") != reference.template_id:
            raise ConfigError(f"{path}: template_id mismatch")
        if str(document.get("template_version")) != reference.template_version:
            raise ConfigError(f"{path}: template_version mismatch")
        return ResolvedTemplate(
            reference=reference,
            path=path,
            content_hash=config_hash(document),
            document=document,
        )
