"""仅注册 File / Modbus / Redis 三种 Sink。"""

from pathlib import Path

import pytest

from collector.infrastructure.sink_registry import build_sink_registry
from core.domain import Sink
from core.infrastructure.sink.file import FileSink
from core.infrastructure.sink.redis import RedisSink


def test_three_builtin_types() -> None:
    assert build_sink_registry().registered_types() == ("file", "modbus", "redis")


def test_file_factory(tmp_path: Path) -> None:
    config = Sink(
        sink_id="file",
        kind="file",
        enabled=True,
        connection={"path": str(tmp_path / "archive")},
    )
    sink = build_sink_registry().create(config)
    assert isinstance(sink, FileSink)


def test_redis_factory() -> None:
    config = Sink(sink_id="redis", kind="redis", enabled=True, connection={})
    sink = build_sink_registry().create(config)
    assert isinstance(sink, RedisSink)


def test_domain_rejects_unsupported_sink_type() -> None:
    with pytest.raises(ValueError, match="unsupported sink type"):
        Sink(sink_id="x", kind="kafka", enabled=True, connection={})
