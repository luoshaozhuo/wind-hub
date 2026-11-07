"""将只读配置对象转换为 YAML 可序列化的基础容器。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from pydantic import SecretStr


def yaml_plain(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: yaml_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [yaml_plain(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return yaml_plain(asdict(value))
    return value
