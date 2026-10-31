"""Collector service configuration validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.errors import ConfigError


def validate_service(document: Mapping[str, Any]) -> None:
    """Reject unknown process-level configuration fields."""
    allowed = {"schema_version", "runtime", "watchdog", "messaging", "metrics", "ads", "logs"}
    unknown = document.keys() - allowed
    if unknown:
        raise ConfigError(f"collector.yaml unknown fields: {sorted(unknown)}")


def validate_sections(document: Mapping[str, Any]) -> None:
    """Validate process configuration sections before constructing a snapshot."""
    sections = {
        "runtime": {
            "shutdown_timeout",
            "thread_pool",
            "connect_timeout",
            "read_timeout",
            "write_timeout",
        },
        "watchdog": {"interval", "health_timeout", "connect_timeout"},
        "messaging": {"servers", "delivery", "max_staleness_ms"},
        "metrics": {
            "enabled",
            "windows",
            "publish_interval",
            "max_staleness",
            "event_capacity",
            "pending_capacity",
        },
    }
    for name, fields in sections.items():
        value = document.get(name, {})
        if not isinstance(value, dict) or set(value) - fields:
            raise ConfigError(f"collector.yaml invalid {name} fields")


def validate_metrics(document: Mapping[str, Any]) -> None:
    """Enforce exact rolling-window aggregation and bounded metrics buffers."""
    raw = document.get("metrics", {})
    if not isinstance(raw, dict):
        raise ConfigError("metrics must be a mapping")
    interval = raw.get("publish_interval", 60)
    windows = raw.get("windows", [60, 300, 3600])
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ConfigError("metrics.publish_interval must be positive integer")
    if not isinstance(windows, list) or not windows:
        raise ConfigError("metrics.windows must be a non-empty list")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value % interval
        for value in windows
    ):
        raise ConfigError("metrics windows must be positive multiples of publish_interval")
    if len(set(windows)) != len(windows):
        raise ConfigError("metrics.windows contains duplicates")
    for field in ("max_staleness", "event_capacity", "pending_capacity"):
        value = raw.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ConfigError(f"metrics.{field} must be positive integer")
    if "enabled" in raw and type(raw["enabled"]) is not bool:
        raise ConfigError("metrics.enabled must be boolean")


def validate_messaging(document: Mapping[str, object]) -> None:
    raw = document.get("messaging")
    if not isinstance(raw, dict):
        raise ConfigError("collector.yaml messaging is required")
    servers = raw.get("servers")
    if not isinstance(servers, list) or not servers:
        raise ConfigError("messaging.servers requires at least one NATS URL")
    if any(
        not isinstance(url, str) or not url.startswith(("nats://", "tls://")) for url in servers
    ):
        raise ConfigError("messaging.servers contains invalid URL")
    delivery = raw.get("delivery")
    if delivery not in ("realtime", "reliable"):
        raise ConfigError("messaging.delivery is invalid")
    if delivery == "realtime":
        value = raw.get("max_staleness_ms")
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 86400000:
            raise ConfigError("realtime messaging requires valid max_staleness_ms")


def validate_timeouts(document: Mapping[str, object]) -> None:
    from math import isfinite

    for section, names in (
        ("runtime", ("shutdown_timeout", "connect_timeout", "read_timeout", "write_timeout")),
        ("watchdog", ("interval", "health_timeout", "connect_timeout")),
    ):
        raw = document.get(section, {})
        if not isinstance(raw, dict):
            raise ConfigError(f"{section} must be a mapping")
        for name in names:
            if name not in raw:
                continue
            value = raw[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value <= 0
            ):
                raise ConfigError(f"{section}.{name} must be finite and positive")
    runtime = document.get("runtime", {})
    if isinstance(runtime, dict):
        pool = runtime.get("thread_pool", {})
        if not isinstance(pool, dict) or set(pool) - {"max_workers"}:
            raise ConfigError("runtime.thread_pool has invalid fields")
        workers = pool.get("max_workers", 32)
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 256:
            raise ConfigError("runtime.thread_pool.max_workers must be in 1..256")
