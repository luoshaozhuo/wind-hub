"""下一代 Sink 配置契约；不修改现有运行时配置解析。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class FileSinkConnection(BaseModel):
    """CSV 文件 Sink；具体追加及轮转策略由 Adapter 实现。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    rotate_daily: bool = True
    buffer_size: int = Field(default=100, ge=1)

    @field_validator("path")
    @classmethod
    def _nonempty_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        return value


class ModbusSinkConnection(BaseModel):
    """对外提供寄存器访问的 Modbus TCP Server。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "0.0.0.0"
    port: int = Field(default=502, ge=1, le=65535)


class RedisSinkConnection(BaseModel):
    """Redis 写入连接；凭据不以明文显示。"""

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
