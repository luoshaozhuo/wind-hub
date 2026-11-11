"""Typed, immutable configuration identity and validation for Sink Service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any


class ProcessRole(StrEnum):
    REALTIME = "realtime"
    RELIABLE = "reliable"


class DesiredState(StrEnum):
    START = "START"
    STOP = "STOP"


class ConfigurationError(ValueError):
    """A supplied snapshot violates the frozen configuration contract."""


class InvalidConfigurationError(ConfigurationError):
    """The request payload itself is invalid; maps to gRPC INVALID_ARGUMENT."""


class RevisionConflictError(ConfigurationError):
    """expected_revision no longer matches the authoritative revision (ABORTED)."""


class WriterRequiredError(ConfigurationError):
    """A mutating call reached a non-writer context; maps to PERMISSION_DENIED."""


class CandidateNotFoundError(ConfigurationError):
    """The candidate id is unknown or expired; maps to gRPC NOT_FOUND."""


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    path: str
    content: bytes

    def __post_init__(self) -> None:
        path = PurePosixPath(self.path)
        if (
            not self.path
            or self.path.startswith("/")
            or "\\" in self.path
            or any(part in ("", ".", "..") for part in self.path.split("/"))
            or path.as_posix() != self.path
        ):
            raise ConfigurationError(f"unsafe relative configuration path: {self.path!r}")
        if not (self.path.startswith("configs/") and self.path.endswith(".yaml")):
            raise ConfigurationError(f"unsupported configuration path: {self.path!r}")


@dataclass(frozen=True, slots=True)
class ValidatedSnapshot:
    revision: str
    files: tuple[SnapshotFile, ...]
    service: dict[str, Any]
    sinks: dict[str, Any]
    deliveries: dict[str, Any]


def expect_mapping(value: object, location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigurationError(f"{location}: expected mapping with string keys")
    return value


def require_keys(
    mapping: dict[str, Any],
    location: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    """Reject unknown and missing configuration fields before using values."""
    actual = set(mapping)
    missing = required - actual
    unknown = actual - required - optional
    if missing or unknown:
        raise ConfigurationError(
            f"{location}: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def expect_positive_int(value: object, location: str, maximum: int | None = None) -> int:
    if type(value) is not int or value <= 0 or (maximum is not None and value > maximum):
        raise ConfigurationError(f"{location}: invalid positive integer")
    return value


def expect_nonempty_str(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{location}: expected non-empty string")
    return value


def require_version(doc: dict[str, Any], location: str) -> None:
    if type(doc.get("schema_version")) is not int or doc["schema_version"] != 1:
        raise ConfigurationError(f"{location}: unsupported schema_version")
    version = expect_nonempty_str(doc.get("file_version"), f"{location}.file_version")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ConfigurationError(f"{location}: invalid file_version")


def managed_path(value: object, location: str, *, directory: bool = False) -> str:
    """Accept a single config-root-relative pathname; never infer a default."""
    path = expect_nonempty_str(value, location)
    parts = path.split("/")
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in ("", ".", "..") for part in parts)
        or PurePosixPath(path).as_posix() != path
        or len(parts) < 2
        or parts[0] != "configs"
        or (not directory and not path.endswith(".yaml"))
    ):
        raise ConfigurationError(f"{location}: unsafe config-root-relative pathname")
    return path


def config_sources(service: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Return explicit Instance file paths and the Template root."""
    sources = expect_mapping(service.get("config_sources"), "service.config_sources")
    if set(sources) != {"instances", "templates"}:
        raise ConfigurationError("config_sources requires instances and templates")
    instances = expect_mapping(sources["instances"], "config_sources.instances")
    if set(instances) != {"sinks", "deliveries"}:
        raise ConfigurationError("config_sources.instances requires sinks and deliveries")
    paths = {
        key: managed_path(value, f"config_sources.instances.{key}")
        for key, value in instances.items()
    }
    if len(set(paths.values())) != len(paths):
        raise ConfigurationError("config_sources Instance paths must differ")
    if any(not path.startswith("configs/instances/") for path in paths.values()):
        raise ConfigurationError("Instance pathname must reside under configs/instances/")
    templates = expect_mapping(sources["templates"], "config_sources.templates")
    if set(templates) != {"root"}:
        raise ConfigurationError("config_sources.templates requires root")
    root = managed_path(templates["root"], "config_sources.templates.root", directory=True)
    if not (root == "configs/templates" or root.startswith("configs/templates/")):
        raise ConfigurationError("Template root must reside under configs/templates/")
    if root in paths.values() or any(path.startswith(root + "/") for path in paths.values()):
        raise ConfigurationError("config_sources Instance path overlaps Template root")
    return paths, root


def validate_service(service: dict[str, Any]) -> None:
    require_version(service, "service")
    require_keys(
        service,
        "service",
        frozenset(
            {
                "schema_version",
                "file_version",
                "service_id",
                "wind_farm_id",
                "deployment_id",
                "config_root",
                "config_sources",
                "grpc",
                "security",
                "nats",
                "runtime",
                "reliable",
                "metrics",
                "config_sync",
                "shutdown",
            }
        ),
    )
    for field in ("service_id", "wind_farm_id", "deployment_id", "config_root"):
        expect_nonempty_str(service.get(field), f"service.{field}")
    config_sources(service)
    if "process_role" in service or "config_writer" in service:
        raise ConfigurationError("Main owns both data processes and configuration")
    grpc = expect_mapping(service.get("grpc"), "service.grpc")
    require_keys(
        grpc,
        "grpc",
        frozenset(
            {
                "listen_host",
                "listen_port",
                "max_active_rpcs",
                "max_request_bytes",
                "default_deadline_ms",
            }
        ),
    )
    expect_nonempty_str(grpc.get("listen_host"), "grpc.listen_host")
    expect_positive_int(grpc.get("listen_port"), "grpc.listen_port", 65535)
    expect_positive_int(grpc.get("max_active_rpcs"), "grpc.max_active_rpcs", 64)
    expect_positive_int(grpc.get("max_request_bytes"), "grpc.max_request_bytes", 2 * 1024**2)
    expect_positive_int(grpc.get("default_deadline_ms"), "grpc.default_deadline_ms", 5000)
    runtime = expect_mapping(service.get("runtime"), "service.runtime")
    require_keys(
        runtime,
        "runtime",
        frozenset(
            {
                "max_inflight_global",
                "max_inflight_per_sink",
                "max_pending_points",
                "io_wait_timeout_ms",
                "max_event_records",
            }
        ),
        frozenset({"watchdog_interval_ms"}),
    )
    watchdog_interval = runtime.get("watchdog_interval_ms", 5000)
    if not expect_positive_int(watchdog_interval, "runtime.watchdog_interval_ms", 60000) >= 500:
        raise ConfigurationError("runtime.watchdog_interval_ms must be >= 500")
    global_limit = expect_positive_int(
        runtime.get("max_inflight_global"), "runtime.max_inflight_global"
    )
    per_sink = expect_positive_int(
        runtime.get("max_inflight_per_sink"), "runtime.max_inflight_per_sink"
    )
    if per_sink > global_limit:
        raise ConfigurationError("runtime.max_inflight_per_sink exceeds global limit")
    expect_positive_int(runtime.get("max_pending_points"), "runtime.max_pending_points")
    expect_positive_int(runtime.get("io_wait_timeout_ms"), "runtime.io_wait_timeout_ms")
    expect_positive_int(runtime.get("max_event_records"), "runtime.max_event_records", 10000)
    metrics = expect_mapping(service.get("metrics"), "service.metrics")
    require_keys(
        metrics,
        "metrics",
        frozenset(
            {
                "metrics_compute_interval_ms",
                "metrics_window_ms",
                "supported_window_ms",
                "max_time_buckets",
                "overrun_policy",
            }
        ),
    )
    interval = expect_positive_int(
        metrics.get("metrics_compute_interval_ms"), "metrics.metrics_compute_interval_ms", 60000
    )
    window = expect_positive_int(
        metrics.get("metrics_window_ms"), "metrics.metrics_window_ms", 3600000
    )
    if interval < 100 or window < 1000 or window < interval:
        raise ConfigurationError("metrics: unsupported interval or window")
    supported = metrics.get("supported_window_ms")
    if (
        not isinstance(supported, list)
        or not supported
        or any(type(item) is not int or item != window for item in supported)
    ):
        raise ConfigurationError("metrics.supported_window_ms must contain configured window")
    buckets = expect_positive_int(metrics.get("max_time_buckets"), "metrics.max_time_buckets", 3600)
    if window % buckets != 0 or window // buckets == 0:
        raise ConfigurationError("metrics: window must be divisible by bucket count")
    if metrics.get("overrun_policy") != "skip":
        raise ConfigurationError("metrics.overrun_policy must be skip")
    reliable = expect_mapping(service.get("reliable"), "service.reliable")
    require_keys(
        reliable,
        "reliable",
        frozenset(
            {
                "max_deliver",
                "ack_wait_seconds",
                "retry_base_ms",
                "retry_max_ms",
                "max_inflight_per_delivery",
                "transient_retry_policy",
                "permanent_error_action",
                "replay_max_inflight",
                "replay_max_rate_per_second",
                "replay_starvation_alarm_seconds",
                "dedup_key",
            }
        ),
    )
    if reliable.get("max_deliver") != -1:
        raise ConfigurationError("reliable.max_deliver must be -1")
    if reliable.get("max_inflight_per_delivery") != 1:
        raise ConfigurationError("reliable.max_inflight_per_delivery must be 1")
    if reliable.get("replay_max_inflight") != 1:
        raise ConfigurationError("reliable.replay_max_inflight must be 1")
    for key in (
        "ack_wait_seconds",
        "retry_base_ms",
        "retry_max_ms",
        "replay_max_rate_per_second",
        "replay_starvation_alarm_seconds",
    ):
        expect_positive_int(reliable.get(key), f"reliable.{key}")
    if reliable["retry_max_ms"] < reliable["retry_base_ms"]:
        raise ConfigurationError("reliable.retry_max_ms below retry_base_ms")
    if reliable.get("transient_retry_policy") != "continuous":
        raise ConfigurationError("reliable.transient_retry_policy must be continuous")
    if reliable.get("permanent_error_action") != "dlq":
        raise ConfigurationError("reliable.permanent_error_action must be dlq")
    if reliable.get("dedup_key") != "sample_id":
        raise ConfigurationError("reliable.dedup_key must be sample_id")
    security = expect_mapping(service.get("security"), "service.security")
    require_keys(
        security,
        "security",
        frozenset({"ca_cert", "server_cert", "server_key", "callers"}),
    )
    for field in ("ca_cert", "server_cert", "server_key"):
        expect_nonempty_str(security.get(field), f"security.{field}")
    callers = security.get("callers")
    if not isinstance(callers, list) or not callers:
        raise ConfigurationError("security.callers: expected non-empty list")
    caller_ids: set[str] = set()
    identities: set[str] = set()
    for index, raw in enumerate(callers):
        location = f"security.callers[{index}]"
        caller = expect_mapping(raw, location)
        require_keys(
            caller,
            location,
            frozenset({"caller_id", "certificate_identity", "permissions"}),
        )
        caller_id = expect_nonempty_str(caller["caller_id"], f"{location}.caller_id")
        identity = expect_nonempty_str(
            caller["certificate_identity"], f"{location}.certificate_identity"
        )
        permissions = caller["permissions"]
        if (
            not isinstance(permissions, list)
            or not permissions
            or any(permission not in ("query", "control", "config") for permission in permissions)
            or len(permissions) != len(set(permissions))
        ):
            raise ConfigurationError(f"{location}.permissions: invalid permission set")
        if caller_id in caller_ids or identity in identities:
            raise ConfigurationError(f"{location}: duplicated caller identity")
        caller_ids.add(caller_id)
        identities.add(identity)
    sync = expect_mapping(service.get("config_sync"), "service.config_sync")
    expect_positive_int(sync.get("apply_timeout_ms"), "config_sync.apply_timeout_ms")
    if set(sync) != {"apply_timeout_ms"}:
        raise ConfigurationError("unsupported config_sync fields")
    shutdown = expect_mapping(service.get("shutdown"), "service.shutdown")
    require_keys(shutdown, "shutdown", frozenset({"grace_seconds"}))
    expect_positive_int(shutdown.get("grace_seconds"), "shutdown.grace_seconds")
    nats = expect_mapping(service.get("nats"), "service.nats")
    require_keys(
        nats,
        "nats",
        frozenset(
            {
                "servers",
                "realtime_subject",
                "reliable_subject",
                "reliable_stream",
                "max_samples_per_batch",
                "max_batch_bytes",
                "dead_letter_stream",
                "dead_letter_subject",
                "history_storage",
                "dlq_storage",
                "history_discard",
                "dlq_discard",
                "dlq_max_bytes",
                "dlq_max_age_seconds",
                "connect_timeout_ms",
                "max_reconnect_interval_ms",
            }
        ),
    )
    if not isinstance(nats.get("servers"), list) or not nats["servers"]:
        raise ConfigurationError("nats.servers must be a non-empty list")
    if any(not isinstance(url, str) or not url.startswith("nats://") for url in nats["servers"]):
        raise ConfigurationError("nats.servers contains an invalid NATS URL")
    if nats.get("realtime_subject") != "wh.rt.batch":
        raise ConfigurationError("realtime NATS subject must match the v2 batch contract")
    if nats.get("reliable_subject") != "wh.hist.batch":
        raise ConfigurationError("reliable NATS subject must match the v2 batch contract")
    expect_positive_int(nats.get("max_samples_per_batch"), "nats.max_samples_per_batch", 10000)
    expect_positive_int(nats.get("max_batch_bytes"), "nats.max_batch_bytes", 16777216)
    for key in ("dlq_max_bytes", "dlq_max_age_seconds"):
        expect_positive_int(nats.get(key), f"nats.{key}")
    for field in ("history_storage", "dlq_storage"):
        if nats.get(field) != "file":
            raise ConfigurationError(f"nats.{field} must be file")
    for field in ("history_discard", "dlq_discard"):
        if nats.get(field) != "new":
            raise ConfigurationError(f"nats.{field} must be new")
