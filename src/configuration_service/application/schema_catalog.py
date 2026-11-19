"""Built-in, versioned YAML document Schema definitions.

Known configuration categories are explicit and do not accept unregistered
document types. Module-specific service parameters use the existing module
validators in addition to this repository boundary check.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SchemaSpec:
    config_type: str
    schema_version: int
    fields: dict[str, Any]
    header: dict[str, Any]


def _field(name: str, kind: str = "object", *, required: bool = False) -> dict[str, Any]:
    return {
        "type": kind,
        "required": required,
        "description": name,
        "range": "由配置 Schema 及模块契约约束",
        "unit": "-",
        "example": "{}" if kind == "object" else "-",
    }


# The first version must continue to accept the repository's existing YAML
# document layouts. Each class's first-party validator can impose tighter
# nested rules without changing the transport or storage schema.
_FIELDS: dict[str, dict[str, str]] = {
    "units": {"quantities": "object", "units": "object"},
    "semantic_points": {"semantic_points": "object"},
    "business_points": {"business_points": "object", "device_type": "string"},
    "point_table": {
        "template_id": "string",
        "device_type": "string",
        "protocol": "string",
        "points": "object",
        "inherits": "object",
        "parent_version_id": "string",
        "parent_point_table_id": "string",
    },
    "device": {"devices": "object", "revision": "string"},
    "device_model": {
        "template_id": "string",
        "name": "string",
        "device_type_id": "string",
        "protocol": "object",
        "point_table": "object",
        "simulation": "object",
        "task": "object",
    },
    "service": {
        "service_id": "string",
        "module_type": "string",
        "runtime": "object",
        "grpc": "object",
        "security": "object",
        "repository": "object",
        "audit": "object",
        "timeouts": "object",
        "shutdown": "object",
        "metrics": "object",
        "watchdog": "object",
        "messaging": "object",
        "dispatcher": "object",
        "servers": "array",
        "nats": "object",
        "reliable": "object",
        "config_sync": "object",
        "service": "object",
        "output": "object",
        "buffer": "object",
        "replay": "object",
        "diagnose": "object",
        "recording": "object",
        "scenarios": "array",
        "wind_farm_id": "string",
        "deployment_id": "string",
        "config_root": "string",
        "cli": "object",
        "services": "object",
        "network_probe": "object",
        "allowed_wind_farms": "array",
        "auth": "object",
        "kafka": "object",
        "tdengine": "object",
        "configuration": "object",
        "query": "object",
        "export": "object",
        "backup": "object",
        "retry": "object",
        "reconciliation": "object",
    },
    "site": {"site_id": "string", "name": "string"},
    "task": {
        "template_id": "string",
        "point_group": "string",
        "interval": "number",
        "desired_state": "string",
        "tasks": "object",
        "revision": "string",
    },
    "delivery": {
        "template_id": "string",
        "delivery_mode": "string",
        "source_filter": "object",
        "output": "object",
        "staleness": "object",
        "deliveries": "object",
        "revision": "string",
        "reliable": "object",
    },
    "sink": {
        "template_id": "string",
        "sink_type": "string",
        "kind": "string",
        "config": "object",
        "connection": "object",
        "sinks": "object",
        "revision": "string",
        "capabilities": "object",
        "output": "object",
    },
    "protocol": {
        "template_id": "string",
        "protocol": "string",
        "protocol_options": "object",
    },
    "observability": {
        "observability": "object",
        "metrics": "object",
        "logs": "object",
        "events": "object",
        "tracing": "object",
    },
    "scenario": {"scenarios": "array"},
}
_REQUIRED: dict[str, tuple[str, ...]] = {
    "units": ("quantities", "units"),
    "semantic_points": ("semantic_points",),
    "business_points": ("business_points",),
    "point_table": ("template_id", "protocol", "points"),
    "device": ("devices",),
    "device_model": ("template_id",),
    "site": ("site_id",),
    "protocol": ("template_id",),
}


_NESTED_FIELDS: dict[str, dict[str, str]] = {
    "runtime": {
        "shutdown_timeout": "number",
        "connect_timeout": "number",
        "read_timeout": "number",
        "thread_pool": "object",
        "max_inflight_global": "integer",
        "max_inflight_per_sink": "integer",
        "max_pending_points": "integer",
        "io_wait_timeout_ms": "integer",
        "max_event_records": "integer",
        "watchdog_interval_ms": "integer",
    },
    "grpc": {
        "listen_host": "string",
        "listen_port": "integer",
        "max_active_rpcs": "integer",
        "request_max_bytes": "integer",
        "max_request_bytes": "integer",
        "default_deadline_ms": "integer",
        "tls": "object",
        "permissions": "object",
    },
    "security": {
        "ca_cert": "string",
        "server_cert": "string",
        "server_key": "string",
        "callers": "array",
        "tls": "object",
        "permissions": "object",
    },
    "repository": {
        "sqlite_path": "string",
        "max_storage_bytes": "integer",
        "reserved_storage_bytes": "integer",
        "retention_seconds": "integer",
        "cleanup_interval_seconds": "integer",
        "storage_warning_ratio": "number",
        "admission_stop_ratio": "number",
        "accept_commit_timeout_ms": "integer",
        "persistence_interval_ms": "integer",
        "persistence_batch_size": "integer",
        "persistence_max_events": "integer",
    },
}


def _nested_definition(group: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = _NESTED_FIELDS[group]
    properties: dict[str, Any] = {}
    header: dict[str, Any] = {}
    for name, kind in fields.items():
        rule: dict[str, Any] = {"type": kind}
        note = _field(name, kind)
        if kind == "object":
            # Module-specific dynamic mappings retain their own field contracts.
            rule["additionalProperties"] = True
        if group == "security" and name == "callers":
            caller_fields = {
                "caller_id": {"type": "string"},
                "certificate_identity": {"type": "string"},
                "permissions": {"type": "array", "items": {"type": "string"}},
            }
            rule["items"] = {
                "type": "object",
                "properties": caller_fields,
                "required": ["caller_id", "certificate_identity", "permissions"],
                "additionalProperties": False,
            }
            note["properties"] = {
                key: _field(key, "array" if key == "permissions" else "string", required=True)
                for key in caller_fields
            }
            note["properties"]["permissions"]["items"] = {
                "type": "string",
                "description": "调用者权限名称",
                "example": "query",
            }
        properties[name] = rule
        header[name] = note
    return properties, header


def iter_schemas() -> Iterator[SchemaSpec]:
    """Return immutable software-controlled type definitions, stable by identity."""
    for config_type, kinds in sorted(_FIELDS.items()):
        properties: dict[str, dict[str, Any]] = {
            "schema_version": {"type": "integer"},
            "file_version": {"type": "string"},
            "config_sources": {
                "type": ["object", "array"],
                "items": {"type": "object"},
            },
        }
        header = {
            "schema_version": _field("配置结构版本 1", "integer", required=True),
            "file_version": _field("业务配置版本 MAJOR.MINOR.PATCH", "string", required=True),
            "config_sources": _field("配置依赖路径或固定版本引用", "object"),
        }
        for name, kind in kinds.items():
            properties[name] = {"type": kind}
            if kind == "array":
                properties[name]["items"] = {"type": "object"}
            header[name] = _field(name, kind, required=name in _REQUIRED.get(config_type, ()))
            if config_type == "service" and name in _NESTED_FIELDS:
                nested_schema, nested_header = _nested_definition(name)
                properties[name]["properties"] = nested_schema
                properties[name]["additionalProperties"] = False
                header[name]["properties"] = nested_header
        yield SchemaSpec(
            config_type=config_type,
            schema_version=1,
            fields={
                "properties": properties,
                "required": [
                    "schema_version",
                    "file_version",
                    *_REQUIRED.get(config_type, ()),
                ],
                "additionalProperties": False,
            },
            header=header,
        )
