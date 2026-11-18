"""Strict YAML import and deterministic documented export."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

import yaml

from configuration_service.domain.models import ConfigIdentity, PreparedDocument

_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


class _Loader(yaml.SafeLoader):
    """Strict YAML 1.2 scalar conventions; aliases are forbidden."""

    yaml_implicit_resolvers = {
        key: [
            (tag, pattern)
            for tag, pattern in resolvers
            if tag
            not in (
                "tag:yaml.org,2002:bool",
                "tag:yaml.org,2002:timestamp",
            )
        ]
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"),
    list("tTfF"),
)


def _mapping(loader: _Loader, node: yaml.MappingNode) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ValueError("YAML mapping keys must be strings")
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _walk(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("YAML contains non-finite numeric value")
        return
    if isinstance(value, list):
        for item in value:
            _walk(item)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("all YAML keys must be strings")
            if any(char in key for char in ("\x00", "\r", "\n")):
                raise ValueError("YAML mapping keys contain forbidden controls")
            _walk(child)
        return
    raise ValueError(f"unsupported YAML value type: {type(value).__name__}")


def parse_yaml(payload: bytes, *, max_bytes: int) -> dict[str, Any]:
    """Load a single UTF-8 YAML mapping without duplicate keys or unsafe tags."""
    if not payload or len(payload) > max_bytes:
        raise ValueError("YAML size is empty or exceeds limit")
    try:
        source = payload.decode("utf-8", errors="strict")
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(source)):
            raise ValueError("YAML aliases are not supported")
        documents = list(yaml.load_all(source, Loader=_Loader))
    except (UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise ValueError("malformed or unsafe YAML input") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError("YAML must contain one root mapping")
    document = documents[0]
    _walk(document)
    if type(document.get("schema_version")) is not int:
        raise ValueError("schema_version must be an integer")
    if document["schema_version"] < 1:
        raise ValueError("schema_version must be positive")
    version = document.get("file_version")
    if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
        raise ValueError("file_version must use MAJOR.MINOR.PATCH")
    return document


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def semantic_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _header_lines(
    properties: Mapping[str, Any],
    *,
    prefix: str = "",
    depth: int = 0,
) -> list[str]:
    result: list[str] = []
    for key in sorted(properties):
        raw = properties[key]
        if not isinstance(raw, dict):
            raise ValueError("header definition must be a mapping of field descriptions")
        location = f"{prefix}.{key}" if prefix else key
        required = "必填" if raw.get("required", False) else "可选"
        kind = raw.get("type", "object")
        description = raw.get("description", "")
        unit = raw.get("unit", "-")
        bounds = raw.get("range", "-")
        example = raw.get("example", "-")
        result.append(
            f"# {location}: {required}; 类型={kind}; 范围={bounds}; "
            f"单位={unit}; 示例={example}; {description}"
        )
        nested = raw.get("properties")
        if isinstance(nested, dict) and depth < 10:
            child_prefix = f"{location}.<item>" if kind == "array" else location
            result.extend(_header_lines(nested, prefix=child_prefix, depth=depth + 1))
        items = raw.get("items")
        if isinstance(items, dict) and depth < 10:
            result.append(
                f"# {location}.<item>: 类型={items.get('type', 'object')}; "
                f"示例={items.get('example', '-')}"
            )
    return result


def _actual_field_notes(value: Mapping[str, Any], *, prefix: str = "", depth: int = 0) -> list[str]:
    """Describe every present nested field without creating undocumented defaults."""
    if depth > 12:
        raise ValueError("configuration nesting depth exceeds Header limit")
    notes: list[str] = []
    for key, child in sorted(value.items()):
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(child, dict):
            notes.append(f"# {path}: mapping；当前已配置 {len(child)} 项")
            notes.extend(_actual_field_notes(child, prefix=path, depth=depth + 1))
        elif isinstance(child, list):
            notes.append(f"# {path}: array；当前已配置 {len(child)} 项")
            records = [item for item in child if isinstance(item, dict)]
            if records:
                fields: dict[str, Any] = {}
                for record in records:
                    for field, field_value in record.items():
                        fields.setdefault(field, field_value)
                notes.extend(_actual_field_notes(fields, prefix=f"{path}.<item>", depth=depth + 1))
        else:
            kind = type(child).__name__ if child is not None else "null"
            examples = {
                "str": "'example'",
                "int": "0",
                "float": "0.0",
                "bool": "false",
                "null": "null",
            }
            notes.append(f"# {path}: 类型={kind}；示例={examples.get(kind, '-')}")
        if len(notes) > 10000:
            raise ValueError("configuration Header exceeds field count limit")
    return notes


def emit_yaml(document: Mapping[str, Any], header_definition: Mapping[str, Any]) -> bytes:
    """Return the single supported format: complete commented Header + YAML body."""
    _walk(dict(document))
    header = [
        "# wind-hub Configuration Service 文档化配置文件",
        "# 文件头由已发布的配置 Schema 自动生成；正文为权威配置值。",
        "# 字段规则（含所有允许字段及动态键规则）：",
        *_header_lines(header_definition),
        "# 当前版本的已配置嵌套字段（Schema 全量声明见上述规则）：",
        *_actual_field_notes(document),
        "",
    ]
    text = yaml.safe_dump(
        dict(document),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
        width=100,
    )
    return ("\n".join(header) + "\n" + text).encode("utf-8")


def prepare_document(
    identity: ConfigIdentity,
    yaml_bytes: bytes,
    *,
    schema_ref: str,
    schema_version: int,
    max_bytes: int,
) -> PreparedDocument:
    document = parse_yaml(yaml_bytes, max_bytes=max_bytes)
    if document["schema_version"] != schema_version:
        raise ValueError("document schema_version does not match registered schema")
    return PreparedDocument(
        identity=identity,
        schema_ref=schema_ref,
        file_version=document["file_version"],
        content_sha256=semantic_hash(document),
        canonical_payload=document,
    )


def reconstruct_document(
    projection_rows: tuple[dict[str, Any], ...],
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    roots = [
        row
        for row in projection_rows
        if row.get("object_type") == "document" and row.get("object_id") == "root"
    ]
    if len(roots) != 1:
        raise RuntimeError("version must contain exactly one canonical document root")
    result = json.loads(roots[0]["properties_json"])
    if not isinstance(result, dict):
        raise RuntimeError("reconstructed document is not a mapping")
    _walk(result)
    if semantic_hash(result) != expected_sha256:
        raise RuntimeError("reconstructed configuration hash mismatch")
    return result
