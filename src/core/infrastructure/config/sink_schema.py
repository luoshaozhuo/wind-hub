"""sinks.yaml 强类型配置模型（Core Infrastructure 层）。

本模块定义 sinks.yaml 的原始配置模型、按类型解析与校验；本模块本身不
创建任何运行时资源。Runtime 对外接口契约（SinkSource / SinkAddress /
ResolvedSinkPoint）属于 Application 层，见
``core.application.sink_contract``。Raw :class:`SinkPoint` 到
:class:`ResolvedSinkPoint` 的 source 引用解析由
``collector.infrastructure.config`` 完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from core.application.errors import ConfigError
from core.application.sink_contract import (
    SINK_DATA_TYPES,
    GrpcSinkAddress,
    ModbusSinkAddress,
    SinkAddress,
    SinkSource,
    StreamSinkAddress,
)

SINK_TYPES = frozenset({"modbus", "redis", "tdengine", "grpc", "kafka"})

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

MODBUS_WORD_WIDTH: Mapping[str, int] = MappingProxyType(
    {
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
    }
)


class RedisSinkConnection(BaseModel):
    """Redis 最新点值输出连接。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    database: int = Field(default=0, ge=0)
    username: str | None = None
    password: SecretStr | None = None
    timeout: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    key_prefix: str = "wind-hub"

    @field_validator("host", "key_prefix")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ConfigError("Redis host/key_prefix must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_acl(self) -> RedisSinkConnection:
        if self.username is not None and self.password is None:
            raise ValueError("Redis ACL username requires password")
        return self

    @field_validator("password", mode="before")
    @classmethod
    def _empty_password_is_none(cls, value: object) -> object:
        # 显式清空（""）与缺省/null 同义，避免空 SecretStr 残留配置文件。
        if isinstance(value, str) and value == "":
            return None
        return value

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("Redis ACL username must be a non-empty trimmed string")
        return value


class TDengineSinkConnection(BaseModel):
    """TDengine taosAdapter REST SQL 连接；database 必须预先创建。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "localhost"
    port: int = Field(default=6041, ge=1, le=65535)
    database: str = "wind_hub"
    stable: str = "point_values"
    username: str = "root"
    password: SecretStr
    timeout: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    tls: bool = False

    @field_validator("database", "stable")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value.isascii() or not value.replace("_", "").isalnum():
            raise ValueError("TDengine identifier must contain only ASCII letters, digits and _")
        if not value or value[0].isdigit():
            raise ValueError("TDengine identifier must start with a letter or _")
        return value

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        if not value or any(char in value for char in "/\\@?#:% \t\r\n"):
            raise ValueError("TDengine host must be a plain hostname or IPv4 address")
        return value

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("TDengine username must be non-empty")
        return value


class ModbusSinkConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    host: str = "0.0.0.0"
    port: int = Field(default=502, ge=1, le=65535)


class GrpcSinkConnection(BaseModel):
    """仅允许同机访问的 Gateway 数据通道。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    endpoint: str = "127.0.0.1:50073"
    timeout: float = Field(default=3.0, gt=0, allow_inf_nan=False)

    @field_validator("endpoint")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if not (value.startswith("127.0.0.1:") or value.startswith("[::1]:")):
            raise ValueError("gRPC endpoint must be loopback")
        return value


class KafkaSinkConnection(BaseModel):
    """Kafka Producer 连接参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bootstrap_servers: str = "127.0.0.1:9092"
    topic: str
    client_id: str = "wind-hub"
    timeout: float = Field(default=10.0, gt=0, allow_inf_nan=False)

    @field_validator("bootstrap_servers", "topic", "client_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Kafka connection field must not be empty")
        return value


SinkConnection = (
    ModbusSinkConnection
    | RedisSinkConnection
    | TDengineSinkConnection
    | GrpcSinkConnection
    | KafkaSinkConnection
)


_CONNECTION_TYPES: dict[str, type[BaseModel]] = {
    "modbus": ModbusSinkConnection,
    "redis": RedisSinkConnection,
    "tdengine": TDengineSinkConnection,
    "grpc": GrpcSinkConnection,
    "kafka": KafkaSinkConnection,
}

_ADDRESS_TYPES: dict[str, tuple[type[BaseModel], ...]] = {
    "modbus": (ModbusSinkAddress,),
    "redis": (StreamSinkAddress,),
    "tdengine": (StreamSinkAddress,),
    "grpc": (GrpcSinkAddress,),
    "kafka": (StreamSinkAddress,),
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


class _SinkSchema(BaseModel):
    """sinks.yaml 中一个完整 Sink 的外部接口契约。

    Sink ID 由 ``sinks`` Mapping 的 Key 提供（见
    :func:`parse_sink_definitions`），条目内部不再重复声明身份。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    enabled: bool = True
    connection: SinkConnection

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
                raise ConfigError(f"invalid sink connection: {exc}") from exc

        return parsed

    @model_validator(mode="after")
    def _validate_sink(self) -> _SinkSchema:
        if self.type not in SINK_TYPES:
            raise ConfigError(f"sink type '{self.type}' must be one of {sorted(SINK_TYPES)}")
        expected_connection = _CONNECTION_TYPES[self.type]
        if not isinstance(self.connection, expected_connection):
            raise ConfigError(f"sink connection does not match type '{self.type}'")

        return self


def validate_task_sink_points(kind: str, points: list[Mapping[str, Any]]) -> None:
    """在 Task 边界沿用 SinkPoint 与地址类型校验，并拒绝重复引用/地址。"""
    if kind not in _ADDRESS_TYPES:
        raise ConfigError(f"unknown Sink type '{kind}'")
    refs: set[str] = set()
    addresses: set[tuple[object, ...]] = set()
    address_type = _ADDRESS_TYPES[kind][0]
    for item in points:
        definition = dict(item)
        if isinstance(definition.get("address"), Mapping):
            definition["address"] = address_type.model_validate(dict(definition["address"]))
        try:
            point = SinkPoint.model_validate(definition)
        except Exception as exc:
            raise ConfigError(f"invalid {kind} Task output point: {exc}") from exc
        ref = point.ref or f"{point.source.device_id}.{point.source.point_id}"
        address = _address_key(point.address)
        if ref in refs:
            raise ConfigError(f"duplicate Task output ref '{ref}'")
        if address in addresses:
            raise ConfigError(f"duplicate Task output address for '{ref}'")
        refs.add(ref)
        addresses.add(address)


def parse_sink_definitions(raw: Mapping[str, Any]) -> dict[str, _SinkSchema]:
    """解析 sinks.yaml 根 Mapping：Mapping Key 即 Sink ID。"""
    if not isinstance(raw, Mapping) or set(raw) - {"sinks"}:
        raise ConfigError("sinks.yaml requires only 'sinks' root")
    entries = raw.get("sinks")
    if not isinstance(entries, Mapping):
        raise ConfigError("sinks must be a mapping keyed by sink ID")
    result: dict[str, _SinkSchema] = {}
    for key, value in entries.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ConfigError("sink IDs must be non-empty trimmed strings")
        try:
            result[key] = _SinkSchema.model_validate(value)
        except ConfigError as exc:
            raise ConfigError(f"Sink '{key}': {exc}") from exc
        except Exception as exc:
            raise ConfigError(f"Sink '{key}' invalid definition: {exc}") from exc
    try:
        validate_sink_definitions(result)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return result


def _address_key(address: SinkAddress) -> tuple[object, ...]:
    if isinstance(address, StreamSinkAddress):
        return ("stream", address.field)
    if isinstance(address, GrpcSinkAddress):
        return ("grpc", address.device_id, address.point_id)
    return ("modbus", address.slave_id, address.register_type, address.address)


__all__ = [
    "SINK_TYPES",
    "SINK_NUMERIC_DATA_TYPES",
    "MODBUS_WORD_WIDTH",
    "ModbusSinkConnection",
    "RedisSinkConnection",
    "TDengineSinkConnection",
    "GrpcSinkConnection",
    "KafkaSinkConnection",
    "SinkPoint",
]


def validate_sink_definitions(sinks: Mapping[str, _SinkSchema]) -> None:
    """检查 Modbus TCP 端口的绑定冲突。"""
    listeners: dict[tuple[str, int], str] = {}
    for sink_id, sink in sinks.items():
        if not sink.enabled or not isinstance(sink.connection, ModbusSinkConnection):
            continue
        host = sink.connection.host
        port = sink.connection.port
        conflict = any(
            used_port == port
            and (used_host == host or used_host in ("0.0.0.0", "::") or host in ("0.0.0.0", "::"))
            for used_host, used_port in listeners
        )
        if conflict:
            raise ValueError(f"Sink '{sink_id}': duplicate Modbus listener {(host, port)}")
        listeners[(host, port)] = sink_id
