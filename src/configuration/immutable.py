"""Immutable configuration values and explicit mutable serialization copies."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


def freeze_document(value: Any) -> Any:
    """Detach nested YAML structures and reject subsequent modifications."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: freeze_document(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(freeze_document(item) for item in value)
    return value


def plain_document(value: Any) -> Any:
    """Return ordinary mappings and lists for JSON/YAML or consumer parsing."""
    if isinstance(value, Mapping):
        return {key: plain_document(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain_document(item) for item in value]
    return value
