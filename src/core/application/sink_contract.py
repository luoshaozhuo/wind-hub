"""Sink 对外接口契约模型（Core Application 层）。

本模块定义 Sink 运行时的稳定外部接口契约：内部点身份（SinkSource）、
外部地址（Stream/Modbus）以及完成 source 引用解析后的运行时点定义
（ResolvedSinkPoint）。这些类型只依赖领域类型与框架级校验，不感知
YAML/文件细节，也不创建任何运行时资源；sinks.yaml 的原始配置模型与
解析逻辑位于 ``core.infrastructure.config.sink_schema``，由后者组合
本模块类型完成原始点到 ResolvedSinkPoint 的解析。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from core.application.errors import ConfigError
from core.domain import DataType
from core.domain.sink import Sink, freeze_sink_value


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


SINK_DATA_TYPES = frozenset(data_type.value for data_type in DataType)


class SinkSource(BaseModel):
    """Sink 点引用的内部稳定身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: str
    point_id: str

    @model_validator(mode="after")
    def _validate_non_empty(self) -> SinkSource:
        if not self.device_id.strip() or not self.point_id.strip():
            raise ConfigError("Sink source device_id/point_id must be non-empty")
        return self


class StreamSinkAddress(BaseModel):
    """Stream 类 Sink（如 Redis）中对外字段名。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str


class GrpcSinkAddress(BaseModel):
    """数据目的标识，不包含工业协议地址。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    device_id: str
    point_id: str


class ModbusSinkAddress(BaseModel):
    """Modbus Sink 对外寄存器地址契约。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slave_id: StrictInt = Field(ge=0, le=255)
    register_type: Literal["holding", "input", "coil", "discrete"]
    address: int = Field(ge=0, le=65535)
    byte_order: Literal["big", "little"] = "big"
    word_order: Literal["big", "little"] = "big"


SinkAddress = StreamSinkAddress | ModbusSinkAddress | GrpcSinkAddress


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
