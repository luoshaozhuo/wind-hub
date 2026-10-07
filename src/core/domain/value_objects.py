"""Shared Domain 值对象。"""

from __future__ import annotations

from enum import StrEnum


class ValueType(StrEnum):
    """业务点标准值类型。"""

    FLOAT = "float"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    STRING = "string"
