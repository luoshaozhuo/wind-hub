"""跨服务 Sink 配置中的源标识、目标地址与数据类型契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from core.application.errors import ConfigError
from core.domain import DataType

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

