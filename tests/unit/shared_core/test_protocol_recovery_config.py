"""system.yaml runtime protocol recovery fields and serialization."""

from __future__ import annotations

import pytest

from core.application.errors import ConfigError
from core.infrastructure.config.codec import dump_system_config, parse_system_config


def test_read_retries_default_and_roundtrip() -> None:
    default = parse_system_config({"runtime": {"read_timeout": 3}})
    assert default.runtime.read_retries == 1
    assert default.runtime.retry_interval == 1.0
    dumped = dump_system_config(default)["runtime"]
    assert "read_retries" not in dumped
    assert "retry_interval" not in dumped
    custom = parse_system_config({
        "runtime": {
            "read_retries": 3,
            "retry_interval": 0.5,
            "connect_timeout": 2.0,
            "read_timeout": 1.0,
            "write_timeout": 4.0,
        }
    })
    assert custom.runtime.read_retries == 3
    assert custom.runtime.retry_interval == 0.5
    dumped_custom = dump_system_config(custom)["runtime"]
    assert dumped_custom["read_retries"] == 3
    assert dumped_custom["retry_interval"] == 0.5


def test_read_retries_unlimited_roundtrip() -> None:
    config = parse_system_config({"runtime": {"read_retries": -1, "retry_interval": 0}})
    assert config.runtime.read_retries == -1
    assert config.runtime.retry_interval == 0.0
    dumped = dump_system_config(config)["runtime"]
    assert dumped["read_retries"] == -1
    assert dumped["retry_interval"] == 0.0


@pytest.mark.parametrize("bad", [-2, True, 1.5, "3"])
def test_read_retries_reject_invalid_values(bad: object) -> None:
    with pytest.raises(ConfigError):
        parse_system_config({"runtime": {"read_retries": bad}})


@pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf"), "1"])
def test_retry_interval_reject_invalid_values(bad: object) -> None:
    with pytest.raises(ConfigError):
        parse_system_config({"runtime": {"retry_interval": bad}})


def test_legacy_reconnect_attempts_key_is_rejected() -> None:
    """旧 reconnect_attempts 配置键不再保留别名，按未知键拒绝。"""
    with pytest.raises(ConfigError):
        parse_system_config({"runtime": {"reconnect_attempts": 3}})
