"""Strict Storage service configuration with bounded resource budgets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _require_identity_text(values: tuple[str, ...]) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("device closure identities must be non-empty strings")


@dataclass(frozen=True, slots=True)
class DeviceModel:
    """设备型号的精确版本及其权威设备类型、完整 PointTable 身份。"""

    config_id: str
    version_id: str
    file_version: str
    device_type_id: str
    point_table_id: str

    def __post_init__(self) -> None:
        _require_identity_text(
            (
                self.config_id,
                self.version_id,
                self.file_version,
                self.device_type_id,
                self.point_table_id,
            )
        )


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    """设备实例文档版本到精确 DeviceModel 的不可变绑定。"""

    wind_farm_id: str
    device_id: str
    device_config_id: str
    device_version_id: str
    device_file_version: str
    site_config_id: str
    site_file_version: str
    model: DeviceModel

    def __post_init__(self) -> None:
        _require_identity_text(
            (
                self.wind_farm_id,
                self.device_id,
                self.device_config_id,
                self.device_version_id,
                self.device_file_version,
            )
        )
        if bool(self.site_config_id) != bool(self.site_file_version):
            raise ValueError("historical Site Instance identity must be complete or absent")

    @property
    def point_table_id(self) -> str:
        return self.model.point_table_id

    @property
    def device_type_id(self) -> str:
        return self.model.device_type_id


class DeviceClosurePins(dict[str, tuple[str, str]]):
    """沿现有 PointTable pin 调用链传递完整设备闭包。"""

    def __init__(
        self,
        point_table_versions: Mapping[str, tuple[str, str]],
        device_bindings: tuple[DeviceBinding, ...] = (),
    ) -> None:
        super().__init__(point_table_versions)
        self.device_bindings = device_bindings


class InstanceSources(_StrictModel):
    site: str = Field(min_length=1, max_length=256)
    devices: str = Field(min_length=1, max_length=256)


class ConfigSources(_StrictModel):
    instances: InstanceSources


class KafkaConfig(_StrictModel):
    bootstrap_servers: str = Field(min_length=1, max_length=2048)
    topic: str = Field(min_length=1, max_length=256)
    group_id: str = Field(min_length=1, max_length=256)
    max_poll_records: int = Field(default=500, ge=1, le=5000)
    max_partition_workers: int = Field(default=4, ge=1, le=64)
    commit_interval_ms: int = Field(default=1000, ge=100, le=10000)
    commit_timeout_ms: int = Field(default=10000, ge=100, le=120000)
    shutdown_drain_timeout_ms: int = Field(default=120000, strict=True, ge=1000, le=600000)


class DatabaseConfig(_StrictModel):
    precision: Literal["us"] = "us"
    duration: str = "30d"
    keep: str = "1095d"
    cachemodel: Literal["none", "last_row", "last_value", "both"] = "none"
    vgroups: int = Field(default=4, strict=True, ge=1)
    buffer_mb: int = Field(default=256, strict=True, ge=1)
    replica: Literal[1] = 1
    comp: Literal[0, 1, 2] = 2
    wal_level: Literal[2] = 2
    wal_fsync_period_ms: Literal[0] = 0

    @model_validator(mode="after")
    def validate_retention(self) -> DatabaseConfig:
        pattern = r"[1-9][0-9]*d"
        if not re.fullmatch(pattern, self.duration) or not re.fullmatch(pattern, self.keep):
            raise ValueError("retention requires positive integer days")
        duration_days = int(self.duration[:-1])
        keep_days = int(self.keep[:-1])
        if keep_days < 3 * duration_days:
            raise ValueError("KEEP must cover at least three DURATION intervals")
        return self


class IngestionConfig(_StrictModel):
    batch_max_rows: int = Field(default=1000, strict=True, ge=1, le=1000)
    batch_max_bytes: int = Field(default=8388608, strict=True, ge=4096, le=8388608)
    batch_wait_ms: int = Field(default=60000, strict=True, ge=0, le=60000)
    buffer_max_rows: int = Field(default=10000, strict=True, ge=100, le=100000)
    buffer_max_bytes: int = Field(default=134217728, strict=True, ge=1048576, le=536870912)
    writer_count: int = Field(default=4, strict=True, ge=1, le=32)
    write_timeout_ms: int = Field(default=10000, strict=True, ge=100, le=120000)

    @model_validator(mode="after")
    def check_budget(self) -> IngestionConfig:
        if self.batch_max_rows > self.buffer_max_rows:
            raise ValueError("batch rows exceed buffer")
        if self.batch_max_bytes >= self.buffer_max_bytes:
            raise ValueError("buffer must include batch and driver overhead")
        return self


class TypeMapping(_StrictModel):
    bool: Literal["BOOL"]
    int8: Literal["TINYINT", "SMALLINT", "INT", "BIGINT"]
    uint8: Literal["TINYINT UNSIGNED", "SMALLINT UNSIGNED", "INT UNSIGNED", "BIGINT UNSIGNED"]
    int16: Literal["SMALLINT", "INT", "BIGINT"]
    uint16: Literal["SMALLINT UNSIGNED", "INT UNSIGNED", "BIGINT UNSIGNED"]
    int32: Literal["INT", "BIGINT"]
    uint32: Literal["INT UNSIGNED", "BIGINT UNSIGNED"]
    int64: Literal["BIGINT"]
    uint64: Literal["BIGINT UNSIGNED"]
    float32: Literal["FLOAT", "DOUBLE"]
    float64: Literal["DOUBLE"]
    string: Literal["VARCHAR"]
    bytes: Literal["VARBINARY"]


class TDengineConfig(_StrictModel):
    endpoint: str = Field(min_length=1, max_length=256)
    credential_ref: str
    database: DatabaseConfig
    max_connections: int = Field(default=8, ge=1, le=64)
    max_inflight_sql: int = Field(default=4, ge=1, le=32)
    max_concurrent_ddl: int = Field(default=2, ge=1, le=8)
    ingestion: IngestionConfig
    type_mapping: TypeMapping

    @field_validator("credential_ref")
    @classmethod
    def check_credential_reference(cls, value: str) -> str:
        if not re.fullmatch(r"env:[A-Z_][A-Z0-9_]*", value):
            raise ValueError("credential_ref must be a controlled env:NAME reference")
        return value

    @model_validator(mode="after")
    def check_limits(self) -> TDengineConfig:
        if self.max_inflight_sql > self.max_connections:
            raise ValueError("max_inflight_sql exceeds max_connections")
        if self.ingestion.writer_count > self.max_inflight_sql:
            raise ValueError("writer_count exceeds max_inflight_sql")
        return self


class ConfigurationConfig(_StrictModel):
    reconcile_interval_s: int = Field(default=60, ge=5, le=3600)
    cache_max_bytes: int = Field(default=268435456, ge=16777216, le=2147483648)


class QueryConfig(_StrictModel):
    max_page_size: int = Field(default=1000, ge=1, le=1000)
    max_range_days: int = Field(default=31, ge=1, le=365)
    max_concurrent: int = Field(default=4, ge=1, le=32)
    timeout_ms: int = Field(default=30000, ge=100, le=120000)


class ExportConfig(_StrictModel):
    root: Path
    max_jobs: int = Field(default=2, ge=1, le=16)
    max_bytes: int = Field(default=1073741824, ge=1048576, le=10737418240)

    @field_validator("root")
    @classmethod
    def absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("export.root must be absolute without parent traversal")
        return value


class BackupConfig(_StrictModel):
    enabled: bool = False
    root: Path
    max_jobs: int = Field(default=1, ge=1, le=4)
    max_bytes: int = Field(default=53687091200, ge=1048576, le=1099511627776)
    timeout_s: int = Field(default=600, ge=30, le=14400)

    @field_validator("root")
    @classmethod
    def absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("backup.root must be absolute without parent traversal")
        return value


class RetryConfig(_StrictModel):
    initial_ms: int = Field(default=500, ge=100, le=60000)
    max_ms: int = Field(default=30000, ge=1000, le=600000)
    max_attempts: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def check_interval(self) -> RetryConfig:
        if self.max_ms < self.initial_ms:
            raise ValueError("max_ms must not be below initial_ms")
        return self


class ReconciliationConfig(_StrictModel):
    auto_alter: bool = False
    allow_retention_shrink: bool = False
    owner_instance_id: str = Field(min_length=1, max_length=256)


class GrpcConfig(_StrictModel):
    listen_host: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    listen_port: int = Field(default=51110, ge=1, le=65535)
    max_active_rpcs: int = Field(default=64, ge=1, le=512)
    allow_unauthenticated_status: bool = False


class CallerPermission(_StrictModel):
    token_env: str
    permissions: list[
        Literal[
            "storage.read",
            "storage.export",
            "storage.backup",
            "storage.inspect",
            "storage.operate",
            "storage.admin",
        ]
    ] = Field(min_length=1)
    wind_farms: list[str] = Field(min_length=1)

    @field_validator("token_env")
    @classmethod
    def validate_token_env(cls, value: str) -> str:
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", value) is None:
            raise ValueError("caller token_env must be an environment variable name")
        return value


class AuthConfig(_StrictModel):
    callers: list[CallerPermission] = Field(min_length=1)


class StorageConfig(_StrictModel):
    schema_version: Literal[6]
    file_version: str
    service_id: str = Field(min_length=1, max_length=256)
    allowed_wind_farms: list[str] = Field(min_length=1)
    config_sources: ConfigSources
    kafka: KafkaConfig
    grpc: GrpcConfig
    auth: AuthConfig
    tdengine: TDengineConfig
    configuration: ConfigurationConfig
    query: QueryConfig
    export: ExportConfig
    backup: BackupConfig
    retry: RetryConfig
    reconciliation: ReconciliationConfig

    @field_validator("file_version")
    @classmethod
    def check_file_version(cls, value: str) -> str:
        if not re.fullmatch(r"\d+\.\d+\.\d+", value):
            raise ValueError("file_version must be MAJOR.MINOR.PATCH")
        return value

    @model_validator(mode="after")
    def check_identity(self) -> StorageConfig:
        if any(
            not farm or len(farm.encode("utf-8")) > 256 for farm in self.allowed_wind_farms
        ) or len(set(self.allowed_wind_farms)) != len(self.allowed_wind_farms):
            raise ValueError("allowed_wind_farms must be unique bounded identities")
        names = [item.token_env for item in self.auth.callers]
        if len(names) != len(set(names)):
            raise ValueError("caller token environment names must be unique")
        if self.service_id != self.reconciliation.owner_instance_id:
            raise ValueError("Storage owner_instance_id must equal service_id in v1")
        if self.kafka.max_poll_records > self.tdengine.ingestion.buffer_max_rows:
            raise ValueError("Kafka fetch records exceed bounded row buffer")
        if self.export.root == self.backup.root:
            raise ValueError("export and backup roots must not be identical")
        return self


def load_storage_config(source: bytes) -> StorageConfig:
    """Parse trusted Configuration bytes; reject duplicate YAML members."""

    class _UniqueLoader(yaml.SafeLoader):
        pass

    def _unique_mapping(
        loader: yaml.SafeLoader, node: yaml.nodes.MappingNode
    ) -> dict[object, object]:
        entries: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            if key in entries:
                raise ValueError(f"duplicate configuration key: {key}")
            entries[key] = loader.construct_object(value_node, deep=True)
        return entries

    _UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)
    data = yaml.load(source.decode("utf-8"), Loader=_UniqueLoader)
    if not isinstance(data, dict):
        raise ValueError("Storage configuration must be a YAML mapping")
    header = {"schema_version", "file_version", "service_id", "config_sources", "service"}
    if set(data) != header or not isinstance(data["service"], dict):
        raise ValueError("Storage Service v6 must use the canonical service envelope")
    parameters = data["service"]
    if set(parameters) & header:
        raise ValueError("Storage Service parameters cannot redefine envelope identity")
    return StorageConfig.model_validate(
        {
            **{key: value for key, value in data.items() if key != "service"},
            **parameters,
        }
    )
