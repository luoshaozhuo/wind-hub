"""Unit tests for Collector-owned service configuration validation."""

from __future__ import annotations

from copy import deepcopy

import pytest

from collector.infrastructure.config.validation import validate_service_settings
from core.application.errors import ConfigError

pytestmark = pytest.mark.unit


def _settings() -> dict[str, object]:
    return {
        "runtime": {"connect_timeout": 2.0, "thread_pool": {"max_workers": 4}},
        "watchdog": {"interval": 1.0, "health_timeout": 0.5},
        "messaging": {
            "servers": ["nats://127.0.0.1:4222"],
            "delivery": "realtime",
            "max_staleness_ms": 1000,
        },
        "metrics": {
            "publish_interval": 60,
            "windows": [60, 300, 3600],
            "pending_capacity": 16,
        },
    }


def test_valid_service_settings() -> None:
    validate_service_settings(_settings())


def test_v2_batch_messaging_limits_accepted() -> None:
    settings = _settings()
    messaging = settings["messaging"]
    assert isinstance(messaging, dict)
    messaging.update(
        {
            "max_samples_per_batch": 100,
            "max_batch_bytes": 1_048_576,
            "queue_capacity": 2048,
            "enqueue_timeout_s": 2.0,
        }
    )
    validate_service_settings(settings)


def test_unknown_messaging_key_rejected() -> None:
    settings = _settings()
    messaging = settings["messaging"]
    assert isinstance(messaging, dict)
    messaging["legacy_batch_seconds"] = 1
    with pytest.raises(ConfigError):
        validate_service_settings(settings)


@pytest.mark.parametrize(
    ("section", "key", "bad"),
    [
        ("runtime", "connect_timeout", 0),
        ("runtime", "read_timeout", float("nan")),
        ("runtime", "shutdown_timeout", True),
        ("watchdog", "interval", -1),
        ("messaging", "delivery", "no-such-mode"),
        ("messaging", "servers", []),
        ("messaging", "max_staleness_ms", 0),
        ("messaging", "max_samples_per_batch", 0),
        ("messaging", "max_samples_per_batch", True),
        ("messaging", "max_batch_bytes", -1),
        ("messaging", "queue_capacity", 1.5),
        ("messaging", "enqueue_timeout_s", 0),
        ("metrics", "publish_interval", 0),
        ("metrics", "windows", [30, 60]),
        ("metrics", "pending_capacity", -1),
        ("metrics", "enabled", "true"),
    ],
)
def test_invalid_service_settings_rejected(section: str, key: str, bad: object) -> None:
    settings = deepcopy(_settings())
    subsection = settings[section]
    assert isinstance(subsection, dict)
    subsection[key] = bad
    with pytest.raises(ConfigError):
        validate_service_settings(settings)
