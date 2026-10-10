"""新 Sink 定义集合验证。"""

import pytest
from pydantic import ValidationError

from core.application.sink_contract import (
    FileSinkConfig,
    FileSinkConnection,
    ModbusSinkConfig,
    ModbusSinkConnection,
    RedisSinkConfig,
    RedisSinkConnection,
)
from core.application.sink_validation import validate_sink_definitions


def test_valid_mixed_sinks() -> None:
    validate_sink_definitions([
        FileSinkConfig(name="archive", connection=FileSinkConnection(path="./data")),
        ModbusSinkConfig(name="modbus", connection=ModbusSinkConnection(port=1502)),
        RedisSinkConfig(name="cache", connection=RedisSinkConnection()),
    ])


def test_duplicate_names_are_rejected() -> None:
    a = FileSinkConfig(name="archive", connection=FileSinkConnection(path="./a"))
    b = FileSinkConfig(name="archive", connection=FileSinkConnection(path="./b"))
    with pytest.raises(ValueError, match="duplicate sink name"):
        validate_sink_definitions([a, b])


def test_duplicate_enabled_listener_is_rejected() -> None:
    a = ModbusSinkConfig(name="a", connection=ModbusSinkConnection(port=1502))
    b = ModbusSinkConfig(name="b", connection=ModbusSinkConnection(port=1502))
    with pytest.raises(ValueError, match="duplicate Modbus listener"):
        validate_sink_definitions([a, b])


def test_disabled_listener_does_not_conflict() -> None:
    a = ModbusSinkConfig(name="a", connection=ModbusSinkConnection(port=1502))
    b = ModbusSinkConfig(
        name="b", enabled=False, connection=ModbusSinkConnection(port=1502)
    )
    validate_sink_definitions([a, b])


def test_redis_rejects_bad_port() -> None:
    with pytest.raises(ValidationError):
        RedisSinkConnection(port=70000)


def test_redis_password_is_redacted() -> None:
    config = RedisSinkConnection(password="secret")
    assert "secret" not in repr(config)
