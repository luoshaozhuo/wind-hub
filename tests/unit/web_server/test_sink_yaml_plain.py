"""Core Sink 嵌套冻结配置可序列化为标准 YAML 映射。"""

from core.domain.sink import Sink
from core.infrastructure.config.yaml import yaml_plain


def test_nested_sink_configuration_round_trip() -> None:
    sink = Sink(
        sink_id="redis",
        kind="redis",
        enabled=True,
        connection={"options": {"names": ["a", "b"]}},
        points=({"mapping": {"source": "p"}},),
    )
    assert yaml_plain(sink.connection) == {
        "options": {"names": ["a", "b"]}
    }
    assert yaml_plain(sink.points) == [
        {"mapping": {"source": "p"}}
    ]
