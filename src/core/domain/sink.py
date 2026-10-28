"""Sink 是配置中可引用的输出目标，不承担连接和 I/O。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


def freeze_sink_value(value: Any) -> Any:
    """递归冻结 Sink 参数，不允许快照内部嵌套列表被改写。"""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_sink_value(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_sink_value(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ValueError(f"unsupported Sink configuration value: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class SinkOutput:
    """Task-scoped output mapping owned by sinks.yaml, not tasks.yaml."""

    key_prefix: str | None = None
    points: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.key_prefix is not None and not self.key_prefix.strip():
            raise ValueError("Sink output key_prefix must not be empty")
        object.__setattr__(self, "points", tuple(freeze_sink_value(point) for point in self.points))


@dataclass(frozen=True, slots=True)
class Sink:
    """输出 Sink 领域定义：只描述目标身份和连接配置。"""

    sink_id: str
    kind: str
    enabled: bool
    connection: Mapping[str, Any]
    outputs: Mapping[str, SinkOutput] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.sink_id, str) or not self.sink_id.strip():
            raise ValueError("sink_id must be non-empty")
        if self.kind not in {"modbus", "redis", "tdengine", "grpc", "kafka"}:
            raise ValueError(f"unsupported sink type '{self.kind}'")
        if not isinstance(self.enabled, bool):
            raise ValueError("sink enabled must be bool")
        object.__setattr__(self, "sink_id", self.sink_id.strip())
        object.__setattr__(self, "connection", freeze_sink_value(self.connection))
        if any(not task_id.strip() for task_id in self.outputs):
            raise ValueError("Sink output task IDs must be non-empty")
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))
