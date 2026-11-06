"""Sink Service 专有连接配置及对外数据类型边界。"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from core.domain import DataType

SINK_NUMERIC_DATA_TYPES = frozenset(
    kind.value for kind in DataType
    if kind.value.startswith(("int", "uint", "float"))
)
MODBUS_WORD_WIDTH: dict[str, int] = {
    "bool": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "int64": 4,
    "uint64": 4,
    "float64": 4,
}


class _Connection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("timeout", check_fields=False)
    @classmethod
    def _finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeout must be a positive finite number")
        return value


class RedisSinkConnection(_Connection):
    host: str = Field(min_length=1)
    port: int = Field(default=6379, ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = None
    database: int = Field(default=0, ge=0)
    key_prefix: str = Field(default="wind-hub", min_length=1)
    timeout: float = 5.0


class TDengineSinkConnection(_Connection):
    host: str = Field(min_length=1)
    port: int = Field(default=6041, ge=1, le=65535)
    username: str = Field(min_length=1)
    password: SecretStr
    database: str = Field(min_length=1)
    stable: str = Field(default="samples", min_length=1)
    tls: bool = False
    timeout: float = 5.0


class KafkaSinkConnection(_Connection):
    bootstrap_servers: str = Field(min_length=1)
    client_id: str = Field(default="wind-hub-sink", min_length=1)
    topic: str = Field(min_length=1)
    timeout: float = 10.0


class GrpcSinkConnection(_Connection):
    endpoint: str = Field(min_length=1)
    timeout: float = 5.0


class ModbusSinkConnection(_Connection):
    host: str = "0.0.0.0"
    port: int = Field(default=502, ge=1, le=65535)


__all__ = [
    "SINK_NUMERIC_DATA_TYPES",
    "MODBUS_WORD_WIDTH",
    "RedisSinkConnection",
    "TDengineSinkConnection",
    "KafkaSinkConnection",
    "GrpcSinkConnection",
    "ModbusSinkConnection",
]
