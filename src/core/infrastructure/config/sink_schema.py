"""共享 Sink 外部接口契约模型（Core Application 层）。

本模块定义 sinks.yaml 的强类型模型：Sink 配置、Runtime 装配与对外接口
契约的唯一配置来源；本模块本身不创建任何运行时资源。Raw
:class:`SinkPoint` 到 :class:`ResolvedSinkPoint` 的 source 引用解析由
``collector.infrastructure.config`` 完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from core.application.errors import ConfigError
from core.domain import DataType

SINK_TYPES = frozenset({"file", "modbus", "redis"})
SINK_DATA_TYPES = frozenset(data_type.value for data_type in DataType)

SINK_NUMERIC_DATA_TYPES = frozenset(
    {
        "float32",
        "float64",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
    }
)

MODBUS_WORD_WIDTH: Mapping[str, int] = MappingProxyType({
    "bool": 1,
    "int8": 1,
    "uint8": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "int64": 4,
    "uint64": 4,
    "float64": 4,
})


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


class FileSinkConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    max_size_mb: float = Field(default=100, gt=0, allow_inf_nan=False)
    max_files: int = Field(default=30, ge=1)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.strip() or Path(value).suffix.lower() not in ("", ".csv"):
            raise ConfigError("File sink path must be a directory or .csv file")
        return value

    @property
    def max_size_bytes(self) -> int:
        return max(1, int(self.max_size_mb * 1024 * 1024))


class RedisSinkConnection(BaseModel):
    """Redis 最新点值输出连接。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    database: int = Field(default=0, ge=0)
    password: SecretStr | None = None
    key_prefix: str = "wind-hub"

    @field_validator("host", "key_prefix")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ConfigError("Redis host/key_prefix must not be empty")
        return value


class ModbusSinkConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    host: str = "0.0.0.0"
    port: int = Field(default=502, ge=1, le=65535)


SinkConnection = FileSinkConnection | ModbusSinkConnection | RedisSinkConnection


class StreamSinkAddress(BaseModel):
    """Stream 类 Sink（如 Redis）中对外字段名。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str


class ModbusSinkAddress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    unit_id: int = Field(ge=0, le=255)
    register_type: Literal["holding", "input", "coil", "discrete"]
    address: int = Field(ge=0, le=65535)
    byte_order: Literal["big", "little"] = "big"
    word_order: Literal["big", "little"] = "big"


SinkAddress = StreamSinkAddress | ModbusSinkAddress

_CONNECTION_TYPES: dict[str, type[BaseModel]] = {
    "file": FileSinkConnection,
    "modbus": ModbusSinkConnection,
    "redis": RedisSinkConnection,
}

_ADDRESS_TYPES: dict[str, tuple[type[BaseModel], ...]] = {
    "file": (StreamSinkAddress,),
    "modbus": (ModbusSinkAddress,),
    "redis": (StreamSinkAddress,),
}


class SinkPoint(BaseModel):
    """sinks.yaml 中的原始外部点定义。

    ref / datatype / unit 允许省略；完整 Config 加载时由 resolve_sinks
    根据 source 绑定的内部点补全。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: SinkSource
    ref: str | None = None
    datatype: str | None = None
    unit: str | None = None
    scale: float = 1.0
    offset: float = 0.0
    address: SinkAddress

    @model_validator(mode="after")
    def _validate_point(self) -> SinkPoint:
        if self.ref is not None and not self.ref.strip():
            raise ConfigError("Sink point ref must be non-empty")
        if self.datatype is not None and self.datatype not in SINK_DATA_TYPES:
            raise ConfigError(
                f"Sink point '{self.ref or self.source.point_id}': "
                f"unknown datatype '{self.datatype}'"
            )
        return self


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


class _SinkSchema(BaseModel):
    """sinks.yaml 中一个完整 Sink 的外部接口契约。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: str
    enabled: bool = True
    connection: SinkConnection
    points: tuple[SinkPoint, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _parse_typed_components(cls, data: object) -> object:
        """先按 Sink.type 解析 connection/address，避免结构相似 union 误判。"""
        if not isinstance(data, dict):
            return data
        sink_type = data.get("type")
        if not isinstance(sink_type, str) or sink_type not in _CONNECTION_TYPES:
            return data

        parsed = dict(data)
        connection = parsed.get("connection")
        if isinstance(connection, dict):
            try:
                parsed["connection"] = _CONNECTION_TYPES[sink_type].model_validate(connection)
            except ConfigError:
                raise
            except Exception as exc:
                raise ConfigError(
                    f"Sink '{data.get('name', '<unnamed>')}' invalid connection: {exc}"
                ) from exc

        address_type = _ADDRESS_TYPES[sink_type][0]
        points = parsed.get("points")
        if isinstance(points, list):
            parsed_points: list[object] = []
            for raw_point in points:
                if not isinstance(raw_point, dict):
                    parsed_points.append(raw_point)
                    continue
                point = dict(raw_point)
                address = point.get("address")
                if isinstance(address, dict):
                    try:
                        point["address"] = address_type.model_validate(address)
                    except ConfigError:
                        raise
                    except Exception as exc:
                        raise ConfigError(
                            f"Sink '{data.get('name', '<unnamed>')}' invalid point address: {exc}"
                        ) from exc
                parsed_points.append(point)
            parsed["points"] = parsed_points
        return parsed

    @model_validator(mode="after")
    def _validate_sink(self) -> SinkConfig:
        if not self.name.strip():
            raise ConfigError("Sink name must be non-empty")
        if self.type not in SINK_TYPES:
            raise ConfigError(
                f"Sink '{self.name}': type '{self.type}' must be one of {sorted(SINK_TYPES)}"
            )
        expected_connection = _CONNECTION_TYPES[self.type]
        if not isinstance(self.connection, expected_connection):
            raise ConfigError(f"Sink '{self.name}': connection does not match type '{self.type}'")

        refs: set[str] = set()
        addresses: set[tuple[object, ...]] = set()
        for point in self.points:
            if point.ref is not None:
                if point.ref in refs:
                    raise ConfigError(f"Sink '{self.name}': duplicate ref '{point.ref}'")
                refs.add(point.ref)

            point_label = point.ref or f"{point.source.device_id}.{point.source.point_id}"
            allowed = _ADDRESS_TYPES[self.type]
            if not isinstance(point.address, allowed):
                raise ConfigError(
                    f"Sink '{self.name}' point '{point_label}': address does not match "
                    f"type '{self.type}'"
                )
            key = _address_key(point.address)
            if key in addresses:
                raise ConfigError(
                    f"Sink '{self.name}' point '{point_label}': duplicate external address"
                )
            addresses.add(key)
        return self


class _ResolvedSinkSchema(SinkConfig):
    """Runtime 直接消费的 Sink 定义；points 已全部解析为稳定引用。"""

    points: list[ResolvedSinkPoint] = Field(default_factory=list)  # type: ignore[assignment]


class _SinksSchema(BaseModel):
    """Raw YAML root model——``sinks.yaml`` 顶层配置（文件级 wrapper）。

    Sink name 唯一性在此校验；resolve 阶段保持 name 不变，因此 resolved
    Sink 集可直接按 name 索引为 ``dict[name, ResolvedSinkConfig]``。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sinks: list[_SinkSchema] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_names(self) -> _SinksSchema:
        names = [sink.name for sink in self.sinks]
        if len(names) != len(set(names)):
            raise ConfigError(f"Duplicate sink names: {names}")
        validate_sink_definitions(self.sinks)
        return self


def _address_key(address: SinkAddress) -> tuple[object, ...]:
    if isinstance(address, StreamSinkAddress):
        return ("stream", address.field)
    return ("modbus", address.unit_id, address.register_type, address.address)


__all__ = [
    "SINK_TYPES",
    "SINK_DATA_TYPES",
    "SINK_NUMERIC_DATA_TYPES",
    "MODBUS_WORD_WIDTH",
    "SinkSource",
    "FileSinkConnection",
    "ModbusSinkConnection",
    "RedisSinkConnection",
    "StreamSinkAddress",
    "ModbusSinkAddress",
    "SinkAddress",
    "SinkPoint",
    "ResolvedSinkPoint",
]




from __future__ import annotations

from collections.abc import Sequence



def validate_sink_definitions(sinks: Sequence[_SinkSchema]) -> None:
    """检查名称重复及 Modbus TCP 端口的绑定冲突。"""
    names: set[str] = set()
    listeners: set[tuple[str, int]] = set()
    for sink in sinks:
        name = sink.name.strip()
        if not name or name in names:
            raise ValueError(f"empty or duplicate sink name: {sink.name}")
        names.add(name)
        if not sink.enabled or not isinstance(sink.connection, ModbusSinkConnection):
            continue
        host = sink.connection.host
        port = sink.connection.port
        if any(
            used_port == port
            and (used_host == host or used_host in ("0.0.0.0", "::")
                 or host in ("0.0.0.0", "::"))
            for used_host, used_port in listeners
        ):
            raise ValueError(f"duplicate Modbus listener: {(host, port)}")
        listeners.add((host, port))
