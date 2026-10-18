"""验证当前 Sink 注册表只暴露受支持的实现。"""

import pytest

from collector.infrastructure.sink_registry import build_sink_registry
from core.application.sink_contract import ResolvedSink
from core.domain import Sink
from core.infrastructure.sink.modbus import ModbusSink
from core.infrastructure.sink.redis import RedisSink
from core.infrastructure.sink.tdengine import TDengineSink


def test_builtin_types() -> None:
    assert build_sink_registry().registered_types() == ("modbus", "redis", "tdengine")


@pytest.mark.parametrize(
    ("kind", "connection", "expected_type"),
    [
        ("modbus", {}, ModbusSink),
        ("redis", {}, RedisSink),
        ("tdengine", {"password": "local-test"}, TDengineSink),
    ],
)
def test_registered_factory(kind: str, connection: dict[str, str], expected_type: type) -> None:
    config = ResolvedSink(
        sink_id=kind, kind=kind, enabled=True, connection=connection
    ) if kind == "modbus" else Sink(
        sink_id=kind, kind=kind, enabled=True, connection=connection
    )
    assert isinstance(build_sink_registry().create(config), expected_type)


def test_domain_rejects_unsupported_sink_type() -> None:
    with pytest.raises(ValueError, match="unsupported sink type"):
        Sink(sink_id="x", kind="kafka", enabled=True, connection={})
