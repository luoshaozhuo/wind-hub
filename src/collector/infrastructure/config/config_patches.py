"""Strict JSON Pointer updates for one Collector configuration document."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from core.application.errors import ConfigError


@dataclass(frozen=True, slots=True)
class ConfigPatch:
    path: str
    operation: str
    value: Any = None


def _tokens(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/") or len(pointer) == 1:
        raise ConfigError("patch path must be a nonempty JSON Pointer")
    tokens: list[str] = []
    for token in pointer[1:].split("/"):
        if "~" in token:
            for position, character in enumerate(token):
                if character == "~" and (
                    position + 1 >= len(token) or token[position + 1] not in "01"
                ):
                    raise ConfigError("invalid JSON Pointer escape")
        decoded = token.replace("~1", "/").replace("~0", "~")
        if not decoded or decoded in {".", ".."}:
            raise ConfigError("invalid JSON Pointer component")
        tokens.append(decoded)
    return tuple(tokens)


def apply_patches(
    document: Mapping[str, Any],
    patches: tuple[ConfigPatch, ...],
) -> dict[str, Any]:
    """Apply all operations to a copy; no partial mutation on failure."""
    if not patches or len(patches) > 256:
        raise ConfigError("patch count must be between 1 and 256")
    result = deepcopy(dict(document))
    for patch in patches:
        tokens = _tokens(patch.path)
        node: dict[str, Any] = result
        for token in tokens[:-1]:
            child = node.get(token)
            if not isinstance(child, dict):
                raise ConfigError(f"missing patch parent: {patch.path}")
            node = child
        key = tokens[-1]
        if patch.operation == "SET":
            if key not in node:
                raise ConfigError(f"unknown patch field: {patch.path}")
            node[key] = deepcopy(patch.value)
        elif patch.operation == "REMOVE":
            if key not in node:
                raise ConfigError(f"missing patch field: {patch.path}")
            del node[key]
        else:
            raise ConfigError(f"invalid patch operation: {patch.operation}")
    return result
