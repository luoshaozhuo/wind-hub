"""统一 Sink 外部接口契约模型。

本模块定义 sinks.yaml 的强类型模型。sinks.yaml 是 Sink 配置、Runtime 装配
与对外接口契约的唯一配置来源；本模块本身不创建任何运行时资源。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wind_hub_core.model.errors import ConfigError

SINK_TYPES = frozenset({"file", "kafka", "db", "iec104", "opcua", "modbus"})
SINK_DATA_TYPES = frozenset(
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
        "bool",
        "str",
    }
)


class SinkSource(BaseModel):
    """Sink 点引用的内部稳定身份。"""

    model_config = ConfigDict(extra="forbid")

    device_id: str
    point_id: str

    @model_validator(mode="after")
    def _validate_non_empty(self) -> "SinkSource":
        if not self.device_id.strip() or not self.point_id.strip():
            raise ConfigError("Sink source device_id/point_id must be non-empty")
        return self


class FileSinkConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.strip():
            raise ConfigError("File sink path must be non-empty")
        return value
    format: Literal["jsonl", "csv"] = "jsonl"
    max_size_mb: float | None = Field(default=None, gt=0)
    max_age_hours: float | None = Field(default=None, gt=0)
    compress: bool = False
    compress_level: int = Field(default=6, ge=1, le=9)
    buffer_size: int = Field(default=100, ge=1)
    flush_interval: float = Field(default=1.0, ge=0)
    write_header: bool = True


class KafkaSinkConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bootstrap_servers: str | list[str]
    topic: str

    @model_validator(mode="after")
    def _validate_required_text(self) -> "KafkaSinkConnection":
        servers = self.bootstrap_servers
        if isinstance(servers, str):
            valid_servers = bool(servers.strip())
        else:
            valid_servers = bool(servers) and all(
                isinstance(item, str) and bool(item.strip()) for item in servers
            )
        if not valid_servers:
            raise ConfigError("Kafka sink bootstrap_servers must be non-empty")
        if not self.topic.strip():
            raise ConfigError("Kafka sink topic must be non-empty")
        return self
    key_field: Literal["device_id", "point_id", "source"] | None = None
    compression_type: Literal["gzip", "snappy", "lz4", "zstd"] | None = None
    acks: Literal["all", 0, 1] = "all"
    batch_size: int = Field(default=16384, ge=1)
    linger_ms: int = Field(default=0, ge=0)


class DatabaseSinkConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    dsn: str
    table: str

    @model_validator(mode="after")
    def _validate_required_text(self) -> "DatabaseSinkConnection":
        if not self.dsn.strip():
            raise ConfigError("Database sink dsn must be non-empty")
        if not self.table.strip():
            raise ConfigError("Database sink table must be non-empty")
        return self
    batch_size: int = Field(default=1000, ge=1)
    create_table: bool = False
    table_schema: dict[str, str] | None = Field(default=None, alias="schema")
    pool_min_size: int = Field(default=1, ge=1)
    pool_max_size: int = Field(default=10, ge=1)
    write_timeout: float = Field(default=5.0, gt=0)

    @model_validator(mode="after")
    def _validate_pool_sizes(self) -> "DatabaseSinkConnection":
        if self.pool_min_size > self.pool_max_size:
            raise ConfigError("Database sink pool_min_size must be <= pool_max_size")
        return self


class IEC104SinkConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "0.0.0.0"
    port: int = Field(default=2404, ge=1, le=65535)
    common_address: int = Field(default=1, ge=0, le=0xFFFF)
    batch_size: int = Field(default=50, ge=1)


class OPCUASinkConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "0.0.0.0"
    port: int = Field(default=4840, ge=1, le=65535)
    endpoint: str = "/wind-hub"
    namespace: int = Field(default=2, ge=1)


class ModbusSinkConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "0.0.0.0"
    port: int = Field(default=502, ge=1, le=65535)


SinkConnection = (
    FileSinkConnection
    | KafkaSinkConnection
    | DatabaseSinkConnection
    | IEC104SinkConnection
    | OPCUASinkConnection
    | ModbusSinkConnection
)


class StreamSinkAddress(BaseModel):
    """File/Kafka/DB 中对外字段名。"""

    model_config = ConfigDict(extra="forbid")
    field: str


class IEC104SinkAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ioa: int = Field(ge=0, le=0xFFFFFF)
    type_id: Literal[
        "M_SP_NA_1",
        "M_DP_NA_1",
        "M_ME_NA_1",
        "M_ME_NB_1",
        "M_ME_NC_1",
        "M_SP_TB_1",
        "M_DP_TB_1",
        "M_ME_TF_1",
    ]


class OPCUASinkAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    browse_name: str | None = None


class ModbusSinkAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_id: int = Field(ge=0, le=255)
    register_type: Literal["holding", "input", "coil", "discrete"]
    address: int = Field(ge=0, le=65535)
    byte_order: Literal["big", "little"] = "big"
    word_order: Literal["big", "little"] = "big"


SinkAddress = StreamSinkAddress | IEC104SinkAddress | OPCUASinkAddress | ModbusSinkAddress


_CONNECTION_TYPES: dict[str, type[BaseModel]] = {
    "file": FileSinkConnection,
    "kafka": KafkaSinkConnection,
    "db": DatabaseSinkConnection,
    "iec104": IEC104SinkConnection,
    "opcua": OPCUASinkConnection,
    "modbus": ModbusSinkConnection,
}

_ADDRESS_TYPES: dict[str, tuple[type[BaseModel], ...]] = {
    "file": (StreamSinkAddress,),
    "kafka": (StreamSinkAddress,),
    "db": (StreamSinkAddress,),
    "iec104": (IEC104SinkAddress,),
    "opcua": (OPCUASinkAddress,),
    "modbus": (ModbusSinkAddress,),
}


class SinkPoint(BaseModel):
    """单个外部 Sink 点定义。"""

    model_config = ConfigDict(extra="forbid")

    source: SinkSource
    ref: str | None = None
    datatype: str = "float32"
    unit: str = "none"
    scale: float = 1.0
    offset: float = 0.0
    address: SinkAddress

    @model_validator(mode="after")
    def _validate_point(self) -> "SinkPoint":
        if self.ref is None:
            self.ref = f"{self.source.device_id}.{self.source.point_id}"
        elif not self.ref.strip():
            raise ConfigError("Sink point ref must be non-empty")
        if self.datatype not in SINK_DATA_TYPES:
            raise ConfigError(
                f"Sink point '{self.ref}': unknown datatype '{self.datatype}'"
            )
        return self


class SinkConfig(BaseModel):
    """sinks.yaml 中一个完整 Sink 的外部接口契约。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    enabled: bool = True
    connection: SinkConnection
    points: list[SinkPoint] = Field(default_factory=list)

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
    def _validate_sink(self) -> "SinkConfig":
        if not self.name.strip():
            raise ConfigError("Sink name must be non-empty")
        if self.type not in SINK_TYPES:
            raise ConfigError(
                f"Sink '{self.name}': type '{self.type}' must be one of {sorted(SINK_TYPES)}"
            )
        expected_connection = _CONNECTION_TYPES[self.type]
        if not isinstance(self.connection, expected_connection):
            raise ConfigError(
                f"Sink '{self.name}': connection does not match type '{self.type}'"
            )

        refs: set[str] = set()
        addresses: set[tuple[object, ...]] = set()
        for point in self.points:
            assert point.ref is not None
            if point.ref in refs:
                raise ConfigError(f"Sink '{self.name}': duplicate ref '{point.ref}'")
            refs.add(point.ref)

            allowed = _ADDRESS_TYPES[self.type]
            if not isinstance(point.address, allowed):
                raise ConfigError(
                    f"Sink '{self.name}' point '{point.ref}': address does not match "
                    f"type '{self.type}'"
                )
            key = _address_key(point.address)
            if key in addresses:
                raise ConfigError(
                    f"Sink '{self.name}' point '{point.ref}': duplicate external address"
                )
            addresses.add(key)
        return self


class SinksConfig(BaseModel):
    """sinks.yaml 顶层配置。"""

    model_config = ConfigDict(extra="forbid")

    sinks: list[SinkConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_names(self) -> "SinksConfig":
        names = [sink.name for sink in self.sinks]
        if len(names) != len(set(names)):
            raise ConfigError(f"Duplicate sink names: {names}")
        return self


def _address_key(address: SinkAddress) -> tuple[object, ...]:
    if isinstance(address, StreamSinkAddress):
        return ("stream", address.field)
    if isinstance(address, IEC104SinkAddress):
        return ("iec104", address.ioa)
    if isinstance(address, OPCUASinkAddress):
        return ("opcua", address.node_id)
    return (
        "modbus",
        address.unit_id,
        address.register_type,
        address.address,
    )


__all__ = [
    "SINK_TYPES",
    "SINK_DATA_TYPES",
    "SinkSource",
    "FileSinkConnection",
    "KafkaSinkConnection",
    "DatabaseSinkConnection",
    "IEC104SinkConnection",
    "OPCUASinkConnection",
    "ModbusSinkConnection",
    "StreamSinkAddress",
    "IEC104SinkAddress",
    "OPCUASinkAddress",
    "ModbusSinkAddress",
    "SinkPoint",
    "SinkConfig",
    "SinksConfig",
]
