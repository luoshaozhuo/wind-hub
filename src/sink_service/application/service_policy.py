"""Validate Sink runtime and reliable-delivery application policies."""

from __future__ import annotations

from typing import Any

from sink_service.application.config_sources import config_sources
from sink_service.domain.config import (
    ConfigurationError,
    expect_mapping,
    expect_nonempty_str,
    expect_positive_int,
    require_keys,
    require_version,
)


def validate_service(service: dict[str, Any]) -> None:
    require_version(service, "service")
    for field in ("service_id", "wind_farm_id", "deployment_id"):
        expect_nonempty_str(service.get(field), f"service.{field}")
    config_sources(service)
    if "process_role" in service or "config_writer" in service:
        raise ConfigurationError("Main owns both data processes and configuration")
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
