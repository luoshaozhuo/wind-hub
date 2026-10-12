"""三类 Sink 的配置合法性及资源冲突测试。"""

from typing import Any

import pytest
from pydantic import ValidationError

from core.application.errors import ConfigError
from core.infrastructure.config.sink_schema import (
    FileSinkConnection,
    RedisSinkConnection,
    _SinkSchema,
    validate_sink_definitions,
)


def sink(kind: str, connection: dict[str, Any], enabled: bool = True) -> _SinkSchema:
    return _SinkSchema.model_validate(
        {"type": kind, "enabled": enabled, "connection": connection}
    )


def test_valid_three_sinks() -> None:
    validate_sink_definitions(
        {
            "file": sink("file", {"path": "./data.csv"}),
            "server": sink("modbus", {"port": 1502}),
            "cache": sink("redis", {}),
        }
    )


def test_duplicate_id_rejected() -> None:
    """Sink ID 由 Mapping Key 提供，重复 Key 在 YAML 加载期被拒绝。"""
    from core.infrastructure.config.yaml import read_yaml_mapping

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sinks.yaml"
        path.write_text(
            "sinks:\n  same:\n    type: file\n    connection: {path: ./a.csv}\n"
            "  same:\n    type: redis\n    connection: {}\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="duplicate YAML key"):
            read_yaml_mapping(path)


def test_wildcard_listener_conflict() -> None:
    with pytest.raises(ValueError, match="duplicate Modbus"):
        validate_sink_definitions(
            {
                "all": sink("modbus", {"host": "0.0.0.0", "port": 1502}),
                "local": sink("modbus", {"host": "127.0.0.1", "port": 1502}),
            }
        )


def test_distinct_ports_and_disabled_sink() -> None:
    validate_sink_definitions(
        {
            "a": sink("modbus", {"port": 1502}),
            "b": sink("modbus", {"port": 1502}, enabled=False),
            "c": sink("modbus", {"port": 1503}),
        }
    )


def test_redis_port_and_credentials() -> None:
    with pytest.raises(ValidationError):
        RedisSinkConnection(port=70000)


def test_csv_limits() -> None:
    with pytest.raises(ValidationError):
        FileSinkConnection(path="./data", max_files=0)
