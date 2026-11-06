"""Web Server 专用配置值展开工具（不包含文件 I/O）。"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import SecretStr


def yaml_plain(value: Any) -> Any:
    """将业务配置的 SecretStr、Enum 和容器转换为基本 YAML 数据。"""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: yaml_plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [yaml_plain(item) for item in value]
    return value
