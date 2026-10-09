"""system.yaml runtime protocol recovery fields and serialization."""

from __future__ import annotations

import pytest

from core.application.errors import ConfigError
from core.infrastructure.config.codec import dump_system_config, parse_system_config


def test_recovery_attempts_default_and_roundtrip() -> None:
    default = parse_system_config({"runtime": {"read_timeout": 3}})
    assert default.runtime.reconnect_attempts == 1
    assert "reconnect_attempts" not in dump_system_config(default)["runtime"]
    custom = parse_system_config({
        "runtime": {
            "reconnect_attempts": 3,
            "connect_timeout": 2.0,
            "read_timeout": 1.0,
            "write_timeout": 4.0,
        }
    })
    assert custom.runtime.reconnect_attempts == 3
    assert dump_system_config(custom)["runtime"]["reconnect_attempts"] == 3


@pytest.mark.parametrize("bad", [-1, True, 1.5, "3"])
def test_reconnect_attempts_reject_invalid_values(bad: object) -> None:
    with pytest.raises(ConfigError):
        parse_system_config({"runtime": {"reconnect_attempts": bad}})
