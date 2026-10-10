"""仅注册 File / Modbus / Redis 三种 Sink。"""

from pathlib import Path

import pytest

from collector.infrastructure.sink import build_sink_registry
from collector.infrastructure.sink.file.csv import FileSink
from collector.infrastructure.sink.redis import RedisSink
from core.application import ConfigError
from core.application.sink_config import (
    FileSinkConnection, RedisSinkConnection, ResolvedSinkConfig,
)


def test_three_builtin_types() -> None:
    assert build_sink_registry().registered_types() == ("file", "modbus", "redis")


def test_file_factory(tmp_path: Path) -> None:
    sink = build_sink_registry().create(ResolvedSinkConfig(
        name="file", type="file",
        connection=FileSinkConnection(path=str(tmp_path / "x.csv")),
    ))
    assert isinstance(sink, FileSink)


def test_redis_factory() -> None:
    sink = build_sink_registry().create(ResolvedSinkConfig(
        name="redis", type="redis", connection=RedisSinkConnection(),
    ))
    assert isinstance(sink, RedisSink)


def test_unregistered_types_rejected() -> None:
    with pytest.raises(ConfigError, match="Unknown sink type"):
        build_sink_registry().create(ResolvedSinkConfig.model_construct(
            name="unsupported", type="kafka",
            connection=RedisSinkConnection(), points=[],
        ))
