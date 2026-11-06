"""辅助将业务配置对象展开为 YAML 基本数据类型。"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import SecretStr


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

