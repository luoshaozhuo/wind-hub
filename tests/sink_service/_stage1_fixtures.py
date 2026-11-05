"""Shared builders for Sink Service stage-one configuration snapshots.

The v1 rewrite stores every snapshot pathname under ``configs/``: two explicit
Service files, flat exact-version templates and two Instance files. The exact
layout mirrors the production ``config_sources`` / ``template_path`` contract so
the fixtures cannot drift into a layout the Writer/Reader would reject.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import yaml

from sink_service.domain.config import SnapshotFile

# Canonical config-root-relative pathnames shared by the whole stage-one suite.
REALTIME_SERVICE = "configs/services/sink-realtime.yaml"
RELIABLE_SERVICE = "configs/services/sink-reliable.yaml"
SINKS_INSTANCE = "configs/instances/sinks.yaml"
DELIVERIES_INSTANCE = "configs/instances/deliveries.yaml"
TEMPLATE_ROOT = "configs/templates"

_CONFIG_SOURCES = {
    "instances": {
        "sinks": SINKS_INSTANCE,
        "deliveries": DELIVERIES_INSTANCE,
    },
    "templates": {"root": TEMPLATE_ROOT},
}

_BASE_SERVICE: dict[str, Any] = {
    "schema_version": 1,
    "file_version": "1.0.0",
    "service_id": "sink-west",
    "wind_farm_id": "farm-a",
    "deployment_id": "prod-a",
    "config_root": "/var/lib/wind-hub/config",
    "config_sources": copy.deepcopy(_CONFIG_SOURCES),
    "grpc": {
        "listen_host": "127.0.0.1",
        "listen_port": 50061,
        "max_active_rpcs": 64,
        "max_request_bytes": 2 * 1024**2,
        "default_deadline_ms": 5000,
    },
    "security": {
        "ca_cert": "tls/ca.crt",
        "server_cert": "tls/sink.crt",
        "server_key": "tls/sink.key",
        "callers": [
            {
                "caller_id": "maintenance",
                "certificate_identity": "spiffe://wind-hub/maintenance",
                "permissions": ["query", "control", "config"],
            }
        ],
    },
    "nats": {
        "servers": ["nats://127.0.0.1:4222"],
        "realtime_subject": "wh.rt",
        "reliable_subject": "wh.hist",
        "reliable_stream": "WH_HISTORY",
        "dead_letter_stream": "WH_DLQ",
        "dead_letter_subject": "wh.dlq",
        "dlq_max_bytes": 1024 * 1024 * 1024,
        "dlq_max_age_seconds": 86400,
        "history_storage": "file",
        "dlq_storage": "file",
        "history_discard": "new",
        "dlq_discard": "new",
    },
    "runtime": {
        "watchdog_interval_ms": 5000,
        "max_inflight_global": 128,
        "max_inflight_per_sink": 8,
        "max_pending_points": 10000,
        "io_wait_timeout_ms": 3000,
        "max_event_records": 10000,
    },
    "reliable": {
        "max_deliver": -1,
        "ack_wait_seconds": 30,
        "retry_base_ms": 1000,
        "retry_max_ms": 60000,
        "max_inflight_per_delivery": 1,
        "transient_retry_policy": "continuous",
        "permanent_error_action": "dlq",
        "replay_max_inflight": 1,
        "replay_max_rate_per_second": 50,
        "replay_starvation_alarm_seconds": 300,
        "dedup_key": "sample_id",
    },
    "metrics": {
        "metrics_compute_interval_ms": 1000,
        "metrics_window_ms": 60000,
        "supported_window_ms": [60000],
        "max_time_buckets": 120,
        "overrun_policy": "skip",
    },
    "config_sync": {
        "poll_interval_ms": 50,
        "apply_timeout_ms": 10000,
        "writer_endpoint": "",
    },
    "shutdown": {"grace_seconds": 1},
}


def service_document(
    *,
    role: str = "realtime",
    writer: bool = True,
    port: int = 50061,
    config_root: str = "/var/lib/wind-hub/config",
    callers: list[dict[str, Any]] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doc = copy.deepcopy(_BASE_SERVICE)
    doc["process_role"] = role
    doc["config_writer"] = writer
    doc["grpc"]["listen_port"] = port
    doc["config_root"] = config_root
    if not writer:
        doc["config_sync"]["writer_endpoint"] = f"127.0.0.1:{50061 if port == 50062 else port - 1}"
    if callers is not None:
        doc["security"]["callers"] = callers
    if overrides:
        doc.update(overrides)
    return doc


_REDIS_TEMPLATE = {
    "schema_version": 1,
    "file_version": "1.0.0",
    "template_id": "redis_realtime",
    "object_version": "1.0.0",
    "kind": "redis",
    "capabilities": {
        "realtime": True,
        "reliable": False,
        "idempotent": False,
        "supports_concurrent_write": True,
        "max_safe_inflight": 8,
    },
    "connection": {"port": 6379},
    "output": {"namespace": "windhub", "write_timeout_ms": 2000},
}

_KAFKA_TEMPLATE = {
    "schema_version": 1,
    "file_version": "1.0.0",
    "template_id": "kafka_history",
    "object_version": "1.0.0",
    "kind": "kafka",
    "capabilities": {
        "realtime": False,
        "reliable": True,
        "idempotent": True,
        "supports_concurrent_write": True,
        "max_safe_inflight": 1,
    },
    "connection": {"bootstrap_servers": ["127.0.0.1:9092"]},
    "output": {
        "topic": "wh.history",
        "write_timeout_ms": 5000,
        "acks": "all",
        "enable_idempotence": True,
        "business_duplicate_policy": "allow_at_least_once",
    },
}

_REALTIME_DELIVERY_TEMPLATE = {
    "schema_version": 1,
    "file_version": "1.0.0",
    "template_id": "realtime_power",
    "object_version": "1.0.0",
    "delivery_mode": "realtime",
    "source_filter": {},
    "output": {
        "policy": "change_with_heartbeat",
        "period_ms": 100,
        "absolute_threshold": 1.0,
        "relative_threshold": 0.01,
    },
    "staleness": {"max_age_ms": 3000},
}

_RELIABLE_DELIVERY_TEMPLATE = {
    "schema_version": 1,
    "file_version": "1.0.0",
    "template_id": "reliable_history",
    "object_version": "1.0.0",
    "delivery_mode": "reliable",
    "source_filter": {},
    "reliable": {"max_deliver": -1, "dead_letter_subject": "wh.dlq"},
}


def template_doc(name: str) -> dict[str, Any]:
    mapping = {
        "redis_realtime": _REDIS_TEMPLATE,
        "kafka_history": _KAFKA_TEMPLATE,
        "realtime_power": _REALTIME_DELIVERY_TEMPLATE,
        "reliable_history": _RELIABLE_DELIVERY_TEMPLATE,
    }
    return copy.deepcopy(mapping[name])


def template_path(name: str, version: str = "1.0.0") -> str:
    """Production layout: ``configs/templates/<family>/<name>_<version>.yaml``."""
    family = {
        "redis_realtime": "sinks",
        "kafka_history": "sinks",
        "realtime_power": "deliveries",
        "reliable_history": "deliveries",
    }[name]
    return f"{TEMPLATE_ROOT}/{family}/{name}_{version}.yaml"


def _delivery_instance(
    delivery_id: str,
    *,
    template: str,
    sink: str,
    desired: str = "START",
    devices: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "template_id": template,
        "template_version": "1.0.0",
        "sink_id": sink,
        "desired_state": desired,
        "source": {
            "wind_farm_id": "farm-a",
            "collector_id": "collector-1",
            "devices": devices if devices is not None else ["WT001", "WT002"],
            "points": ["active_power"],
        },
        "overrides": {},
    }


def snapshot_docs(
    *,
    writer_port: int = 50061,
    reader_port: int = 50062,
    config_root: str = "/var/lib/wind-hub/config",
    callers: list[dict[str, Any]] | None = None,
    deliveries: dict[str, dict[str, Any]] | None = None,
    sinks: dict[str, dict[str, Any]] | None = None,
    include_templates: tuple[str, ...] = (
        "redis_realtime",
        "kafka_history",
        "realtime_power",
        "reliable_history",
    ),
) -> dict[str, dict[str, Any]]:
    """Return path -> YAML document for a complete valid shared snapshot."""
    realtime_callers = callers
    docs = {
        REALTIME_SERVICE: service_document(
            role="realtime",
            writer=True,
            port=writer_port,
            config_root=config_root,
            callers=realtime_callers,
        ),
        RELIABLE_SERVICE: service_document(
            role="reliable",
            writer=False,
            port=reader_port,
            config_root=config_root,
            callers=callers,
        ),
    }
    for name in include_templates:
        doc = template_doc(name)
        docs[template_path(name)] = doc
    default_sinks = {
        "ems_redis": {
            "template_id": "redis_realtime",
            "template_version": "1.0.0",
            "connection": {"host": "192.168.1.10", "port": 6379},
            "overrides": {},
        },
        "history_kafka": {
            "template_id": "kafka_history",
            "template_version": "1.0.0",
            "connection": {"bootstrap_servers": ["192.168.1.20:9092"]},
            "overrides": {},
        },
    }
    docs[SINKS_INSTANCE] = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "revision": "sinks-r1",
        "sinks": sinks if sinks is not None else default_sinks,
    }
    default_deliveries = {
        "ems_power": _delivery_instance("ems_power", template="realtime_power", sink="ems_redis"),
        "power_history": _delivery_instance(
            "power_history", template="reliable_history", sink="history_kafka"
        ),
    }
    docs[DELIVERIES_INSTANCE] = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "revision": "deliveries-r1",
        "deliveries": deliveries if deliveries is not None else default_deliveries,
    }
    return docs


def snapshot_files(docs: dict[str, dict[str, Any]]) -> tuple[SnapshotFile, ...]:
    return tuple(
        SnapshotFile(path, yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode())
        for path, doc in sorted(docs.items())
    )


def write_tree(root: Path, docs: dict[str, dict[str, Any]]) -> None:
    for path, doc in docs.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode())


def wire_files(
    files: tuple[SnapshotFile, ...],
) -> list[tuple[str, bytes, int, str]]:
    return [
        (
            item.path,
            item.content,
            len(item.content),
            hashlib.sha256(item.content).hexdigest(),
        )
        for item in files
    ]
