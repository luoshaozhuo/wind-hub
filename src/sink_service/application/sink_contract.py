"""Sink Service 运行时契约。

源引用和地址类型由 configuration.sink_address 定义；本模块仅扩展
运行时已解析的 Sink 映射，不包含 YAML 解析或基础设施 I/O。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from core.application.errors import ConfigError
from configuration.sink import Sink, freeze_sink_value
from configuration.sink_address import (
    SINK_DATA_TYPES,
    SinkSource,
    StreamSinkAddress,
    GrpcSinkAddress,
    ModbusSinkAddress,
    SinkAddress,
)


@dataclass(frozen=True, slots=True)
class ResolvedSink(Sink):
    """Collector 装配期生成的 Sink 运行时描述，不属于 Domain Sink。"""

    points: tuple[Mapping[str, Any], ...] = ()
    task_sources: Mapping[str, tuple[tuple[str, str], ...]] | None = None
    task_key_prefixes: Mapping[str, str] | None = None
    task_refs: Mapping[str, tuple[str, ...]] | None = None

    def __post_init__(self) -> None:
        super(ResolvedSink, self).__post_init__()
        object.__setattr__(
            self,
            "points",
            tuple(freeze_sink_value(point) for point in self.points),
        )
        if self.task_sources is not None:
            object.__setattr__(self, "task_sources", freeze_sink_value(self.task_sources))
        if self.task_key_prefixes is not None:
            object.__setattr__(self, "task_key_prefixes", freeze_sink_value(self.task_key_prefixes))
        if self.task_refs is not None:
            object.__setattr__(self, "task_refs", freeze_sink_value(self.task_refs))


class ResolvedSinkPoint(BaseModel):
    """完成 source 引用解析后的运行时 Sink 点定义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: SinkSource
    ref: str
    source_data_type: str
    source_unit: str
    datatype: str
    unit: str
    scale: float = 1.0
    offset: float = 0.0
    address: SinkAddress

    @model_validator(mode="after")
    def _validate_resolved(self) -> ResolvedSinkPoint:
        if not self.ref.strip():
            raise ConfigError("Resolved sink point ref must be non-empty")
        if self.source_data_type not in SINK_DATA_TYPES:
            raise ConfigError(
                f"Resolved sink point '{self.ref}': unknown source_data_type "
                f"'{self.source_data_type}'"
            )
        if self.datatype not in SINK_DATA_TYPES:
            raise ConfigError(
                f"Resolved sink point '{self.ref}': unknown datatype '{self.datatype}'"
            )
        return self


__all__ = [
    "SINK_DATA_TYPES",
    "SinkSource",
    "StreamSinkAddress",
    "ModbusSinkAddress",
    "GrpcSinkAddress",
    "SinkAddress",
    "ResolvedSinkPoint",
]
