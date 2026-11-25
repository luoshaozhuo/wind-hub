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
    "business_points": {"business_points": "object"},
    "point_table": {
        "template_id": "string",
        "device_type": "string",
        "protocol": "string",
        "points": "object",
        "parent": "object",
        "remove_points": "array",
    },
    "device": {"devices": "object", "revision": "string"},
    "device_model": {
        "template_id": "string",
        "name": "string",
        "device_type_id": "string",
        "protocol": "object",
        "point_table": "object",
        "simulation": "object",
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
        "wind_farm_id": "string",
        "deployment_id": "string",
        "config_root": "string",
        "cli": "object",
        "services": "object",
        "tls_profiles": "object",
        "network_probe": "object",
    },
    "site": {"site_id": "string", "name": "string"},
    "collector_tasks": {"tasks": "object"},
    "deployment": {
        "deployment_id": "string",
        "services": "object",
        "instances": "object",
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
    "business_points": ("business_points",),
    "point_table": ("template_id", "protocol", "points"),
    "device": ("devices",),
    "device_model": ("template_id",),
    "site": ("site_id",),
    "collector_tasks": ("tasks",),
    "deployment": ("deployment_id", "services", "instances"),
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
                properties[name]["items"] = {
                    "type": "string" if name == "remove_points" else "object"
                }
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
        if config_type == "service":
            from copy import deepcopy

            updated = deepcopy(properties)
            updated_header = deepcopy(header)
            caller_schema = updated["security"]["properties"]["callers"]["items"]
            caller_fields = {
                "caller_id": {"type": "string"},
                "token_env": {"type": "string"},
                "permissions": {"type": "array", "items": {"type": "string"}},
            }
            caller_schema["properties"] = caller_fields
            caller_schema["required"] = ["caller_id", "token_env", "permissions"]
            caller_header = updated_header["security"]["properties"]["callers"]
            caller_header["properties"] = {
                key: _field(
                    key,
                    "array" if key == "permissions" else "string",
                    required=True,
                )
                for key in caller_fields
            }
            caller_header["properties"]["permissions"]["items"] = {
                "type": "string",
                "description": "调用者权限名称",
                "example": "query",
            }
            updated_header["schema_version"] = _field("配置结构版本 2", "integer", required=True)
            yield SchemaSpec(
                config_type="service",
                schema_version=2,
                fields={
                    "properties": updated,
                    "required": ["schema_version", "file_version"],
                    "additionalProperties": False,
                },
                header=updated_header,
            )

            # V1/V2 are immutable published identities. V3 is a new,
            # token-based contract for controlled simulation deployments.
            successor = deepcopy(updated)
            successor_header = deepcopy(updated_header)
            successor["grpc"]["properties"]["sink_listener_port"] = {
                "type": "integer",
                "minimum": 1,
                "maximum": 65535,
            }
            successor_header["grpc"]["properties"]["sink_listener_port"] = _field(
                "Gateway loopback Sink listener; 1..65535; not the management port",
                "integer",
            )
            for obsolete in ("tls", "permissions"):
                successor["grpc"]["properties"].pop(obsolete, None)
                successor_header["grpc"]["properties"].pop(obsolete, None)
            for obsolete in ("ca_cert", "server_cert", "server_key", "tls"):
                successor["security"]["properties"].pop(obsolete, None)
                successor_header["security"]["properties"].pop(obsolete, None)
            token_reference = {
                "type": "string",
                "pattern": r"[A-Z_][A-Z0-9_]*",
            }
            caller_schema_v3 = successor["security"]["properties"]["callers"]["items"]
            caller_schema_v3["properties"]["token_env"] = token_reference.copy()
            caller_schema_v3["properties"]["caller_id"]["minLength"] = 1
            target_fields = {
                "gateway_id": {"type": "string", "minLength": 1},
                "endpoint": {"type": "string", "pattern": r"[^\s:]+:[0-9]{1,5}"},
                "token_env": token_reference.copy(),
            }
            successor["simulation_targets"] = {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": target_fields,
                    "required": list(target_fields),
                    "additionalProperties": False,
                },
            }
            successor_header["simulation_targets"] = {
                **_field("Commander simulation device to Gateway target", "object"),
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        name: _field(name, "string", required=True) for name in target_fields
                    },
                },
            }
            successor_header["schema_version"] = _field(
                "配置结构版本 3（仅令牌身份）", "integer", required=True
            )
            yield SchemaSpec(
                config_type="service",
                schema_version=3,
                fields={
                    "properties": successor,
                    "required": ["schema_version", "file_version"],
                    "additionalProperties": False,
                },
                header=successor_header,
            )


            # Keep published V1–V3 definitions immutable for historical retrieval.
            # New service envelopes share the repository category, not a runtime model.
            wide = deepcopy(successor)
            wide_header = deepcopy(successor_header)
            wide["security"]["properties"]["callers"]["items"]["properties"]["token_env"] = {
                "type": "string",
                "pattern": r"env:[A-Z_][A-Z0-9_]*",
            }
            wide_header["schema_version"] = _field(
                "配置结构版本 6（宽表 Storage / Collector / Sink）",
                "integer",
                required=True,
            )
            yield SchemaSpec(
                config_type="service",
                schema_version=6,
                fields={
                    "properties": wide,
                    "required": ["schema_version", "file_version", "service_id"],
                    "additionalProperties": False,
                },
                header=wide_header,
            )
        elif config_type == "sink":
            from copy import deepcopy

            wide = deepcopy(properties)
            wide_header = deepcopy(header)
            wide["kind"]["enum"] = ["kafka"]
            wide["output"] = {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "minLength": 1},
                    "key_field": {"type": "string", "enum": ["wind_farm_device"]},
                    "business_duplicate_policy": {
                        "type": "string",
                        "enum": ["allow_at_least_once"],
                    },
                    "dedup_identity_fields": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["wind_farm_id", "device_id", "data_level", "sample_time"],
                        },
                    },
                    "enable_idempotence": {"type": "boolean", "enum": [True]},
                    "acks": {"type": "string", "enum": ["all"]},
                    "write_timeout_ms": {"type": "integer", "minimum": 1, "maximum": 30000},
                },
                "required": [
                    "topic", "key_field", "business_duplicate_policy", "dedup_identity_fields",
                    "enable_idempotence", "acks", "write_timeout_ms",
                ],
                "additionalProperties": False,
            }
            wide_header["schema_version"] = _field(
                "Kafka 宽表输出结构版本 2", "integer", required=True
            )
            yield SchemaSpec(
                config_type="sink",
                schema_version=2,
                fields={
                    "properties": wide,
                    "required": [
                        "schema_version", "file_version", "template_id", "kind",
                        "capabilities", "connection", "output",
                    ],
                    "additionalProperties": False,
                },
                header=wide_header,
            )
