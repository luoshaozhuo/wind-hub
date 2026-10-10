"""三类 Sink 的配置合法性及资源冲突测试。"""

import pytest
from pydantic import ValidationError

from core.infrastructure.config.sink_schema import (
    FileSinkConnection,
    RedisSinkConnection,
    _SinkSchema,
)
from core.infrastructure.config.sink_schema import validate_sink_definitions


def sink(name: str, kind: str, connection: dict, enabled: bool = True) -> _SinkSchema:
    return _SinkSchema.model_validate({
        "name": name, "type": kind, "enabled": enabled, "connection": connection
    })


def test_valid_three_sinks() -> None:
    validate_sink_definitions([
        sink("file", "file", {"path": "./data.csv"}),
        sink("server", "modbus", {"port": 1502}),
        sink("cache", "redis", {}),
    ])


def test_duplicate_name_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        validate_sink_definitions([
            sink("same", "file", {"path": "./a.csv"}),
            sink("same", "redis", {}),
        ])


def test_wildcard_listener_conflict() -> None:
    with pytest.raises(ValueError, match="duplicate Modbus"):
        validate_sink_definitions([
            sink("all", "modbus", {"host": "0.0.0.0", "port": 1502}),
            sink("local", "modbus", {"host": "127.0.0.1", "port": 1502}),
        ])


def test_distinct_ports_and_disabled_sink() -> None:
    validate_sink_definitions([
        sink("a", "modbus", {"port": 1502}),
        sink("b", "modbus", {"port": 1502}, enabled=False),
        sink("c", "modbus", {"port": 1503}),
    ])


def test_redis_port_and_credentials() -> None:
    with pytest.raises(ValidationError):
        RedisSinkConnection(port=70000)


def test_csv_limits() -> None:
    with pytest.raises(ValidationError):
        FileSinkConnection(path="./data", max_files=0)
