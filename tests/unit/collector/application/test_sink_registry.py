"""SinkRegistry 与内置 Sink 显式注册 builder 的单元测试。

架构约束与 ProtocolRegistry 一致：注册表无共享全局实例，内置 Sink 的唯一
注册路径是组合根显式调用的 ``build_sink_registry()``。

配置模型按 ``type`` 判别 connection 联合，非法类型无法通过校验——因此
「未知类型」语义用「合法类型但未在当前注册表登记」覆盖。
"""

from __future__ import annotations

import pytest

from wind_hub_collector.adapter.outbound.sink import build_sink_registry
from wind_hub_collector.application.port.sink_registry import SinkRegistry
from wind_hub_core.config import ResolvedSinkConfig
from wind_hub_core.model.errors import ConfigError


def _make_sink_cfg(name: str = "s1", type_: str = "file") -> ResolvedSinkConfig:
    return ResolvedSinkConfig(
        name=name,
        type=type_,
        connection={"path": "/tmp/wind-hub-test-sink-registry/points.jsonl"},
    )


class TestFreshRegistry:
    """空注册表的行为契约；测试自行注册 fake factory。"""

    def test_register_and_create(self) -> None:
        reg = SinkRegistry()
        reg.register("file", lambda cfg: f"sink-{cfg.name}")  # type: ignore[arg-type,return-value]
        assert reg.create(_make_sink_cfg()) == "sink-s1"

    def test_register_duplicate_raises(self) -> None:
        reg = SinkRegistry()
        reg.register("file", lambda cfg: "x")  # type: ignore[arg-type,return-value]
        with pytest.raises(ConfigError, match="already registered"):
            reg.register("file", lambda cfg: "y")  # type: ignore[arg-type,return-value]

    def test_create_unknown_lists_registered_types(self) -> None:
        reg = SinkRegistry()
        reg.register("modbus", lambda cfg: "x")  # type: ignore[arg-type,return-value]
        with pytest.raises(ConfigError, match=r"Unknown sink type 'file'.*modbus"):
            reg.create(_make_sink_cfg())

    def test_registered_types_snapshot(self) -> None:
        reg = SinkRegistry()
        reg.register("b", lambda cfg: "x")  # type: ignore[arg-type,return-value]
        reg.register("a", lambda cfg: "x")  # type: ignore[arg-type,return-value]
        assert reg.registered_types() == ("a", "b")


class TestBuiltinBuilder:
    """内置类型完全经显式 builder 注册，新增类型不需要改中央分派链。"""

    def test_build_registers_builtin_types(self) -> None:
        assert build_sink_registry().registered_types() == (
            "db",
            "file",
            "iec104",
            "kafka",
            "modbus",
        )

    def test_build_returns_independent_instances(self) -> None:
        first = build_sink_registry()
        second = build_sink_registry()
        first.register("opcua", lambda cfg: "x")  # type: ignore[arg-type,return-value]
        assert "opcua" not in second.registered_types()

    def test_create_file_sink(self, tmp_path) -> None:
        from wind_hub_collector.adapter.outbound.sink.file.csv import FileSink

        cfg = ResolvedSinkConfig(
            name="file_sink",
            type="file",
            connection={"path": str(tmp_path / "points.jsonl")},
        )
        sink = build_sink_registry().create(cfg)
        assert isinstance(sink, FileSink)
