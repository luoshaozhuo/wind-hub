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


def test_modbus_config_accepts_mapping_dict() -> None:
    cfg = ModbusSinkConnection.model_validate({
        "points": [{
            "source": {"device_id": "WT001", "point_id": "power"},
            "address": {
                "unit_id": 1,
                "register_type": "holding",
                "address": 100,
                "data_type": "float32",
            },
        }],
    })
    assert cfg.points[0].address.width == 2


def test_modbus_config_rejects_overlapping_registers() -> None:
    points = [
        {
            "source": {"device_id": "WT001", "point_id": point_id},
            "address": {
                "unit_id": 1,
                "register_type": "holding",
                "address": address,
                "data_type": "float32",
            },
        }
        for point_id, address in [("power", 100), ("speed", 101)]
    ]
    with pytest.raises(ValidationError, match="overlapping"):
        ModbusSinkConnection.model_validate({"points": points})


@pytest.mark.parametrize("field", ["host", "key_prefix"])
def test_redis_rejects_empty_host_or_prefix(field: str) -> None:
    with pytest.raises(ValidationError):
        RedisSinkConnection.model_validate({field: " "})
