"""Infrastructure transport, security and telemetry schema validation for Sink Service."""

from __future__ import annotations

import re
from typing import Any

from sink_service.domain.config import (
    ConfigurationError,
    expect_mapping,
    expect_nonempty_str,
    expect_positive_int,
    require_keys,
)


def validate_service_technology(service: dict[str, Any]) -> None:
    """Check deployment-specific transports, credentials and metrics configuration."""
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
    expect_nonempty_str(service.get("config_root"), "service.config_root")
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
    security = expect_mapping(service.get("security"), "service.security")
    require_keys(
        security,
        "security",
        frozenset({"callers"}),
    )
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
            frozenset({"caller_id", "token_env", "permissions"}),
        )
        caller_id = expect_nonempty_str(caller["caller_id"], f"{location}.caller_id")
        identity = expect_nonempty_str(
            caller["token_env"], f"{location}.token_env"
        )
        if re.fullmatch(r"env:[A-Z_][A-Z0-9_]*", identity) is None:
            raise ConfigurationError(f"{location}.token_env must reference env:NAME")
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
