"""三类 Sink 的统一、强类型配置契约。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class FileSinkConnection(BaseModel):
    """CSV 文件：UTC 每日或达到容量上限时轮转。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    max_size_mb: float = Field(default=100.0, gt=0, allow_inf_nan=False)
    max_files: int = Field(default=30, ge=1)
    buffer_size: int = Field(default=100, ge=1)
    flush_interval: float = Field(default=1.0, ge=0, allow_inf_nan=False)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.strip() or Path(value).suffix.lower() not in ("", ".csv"):
            raise ValueError("path must be a directory or .csv file")
        return value

    @property
    def max_size_bytes(self) -> int:
        return max(1, int(self.max_size_mb * 1024 * 1024))


class ModbusSinkConnection(BaseModel):
    """Modbus TCP Server 监听地址。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "0.0.0.0"
    port: int = Field(default=502, ge=1, le=65535)


class RedisSinkConnection(BaseModel):
    """Redis 连接与键前缀。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    database: int = Field(default=0, ge=0)
    password: SecretStr | None = None
    key_prefix: str = "wind-hub"


class FileSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    type: Literal["file"] = "file"
    enabled: bool = True
    connection: FileSinkConnection


class ModbusSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    type: Literal["modbus"] = "modbus"
    enabled: bool = True
    connection: ModbusSinkConnection


class RedisSinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    type: Literal["redis"] = "redis"
    enabled: bool = True
    connection: RedisSinkConnection


SinkConfig = Annotated[
    FileSinkConfig | ModbusSinkConfig | RedisSinkConfig,
    Field(discriminator="type"),
]
