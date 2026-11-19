"""Strict Storage service configuration with bounded resource budgets."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class KafkaConfig(_StrictModel):
    topic: str = Field(min_length=1, max_length=256)
    group_id: str = Field(min_length=1, max_length=256)
    max_poll_records: int = Field(default=500, ge=1, le=5000)
    max_partition_workers: int = Field(default=4, ge=1, le=64)
    max_buffer_records: int = Field(default=20000, ge=100, le=100000)
    max_buffer_bytes: int = Field(default=67108864, ge=1048576, le=536870912)
    commit_interval_ms: int = Field(default=1000, ge=100, le=10000)


class DatabaseConfig(_StrictModel):
    precision: Literal["us"] = "us"
    duration: str = "10d"
    keep: str = "365d"
    cachemodel: Literal["none", "last_row", "last_value", "both"] = "none"
    cachesize: int = Field(default=1, ge=1, le=1024)

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


class TDengineConfig(_StrictModel):
    endpoint: str = Field(min_length=1, max_length=256)
    credential_ref: str
    database: DatabaseConfig
    max_connections: int = Field(default=8, ge=1, le=64)
    max_inflight_sql: int = Field(default=4, ge=1, le=32)
    max_concurrent_ddl: int = Field(default=2, ge=1, le=8)
    batch_max_records: int = Field(default=1000, ge=1, le=10000)
    batch_max_bytes: int = Field(default=1048576, ge=4096, le=8388608)
    batch_wait_ms: int = Field(default=20, ge=0, le=1000)
    write_timeout_ms: int = Field(default=10000, ge=100, le=120000)

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
    root: Path
    max_jobs: int = Field(default=1, ge=1, le=4)

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


class StorageConfig(_StrictModel):
    schema_version: Literal[1]
    file_version: str
    service_id: str = Field(min_length=1, max_length=256)
    kafka: KafkaConfig
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
        if self.service_id != self.reconciliation.owner_instance_id:
            raise ValueError("Storage owner_instance_id must equal service_id in v1")
        if self.tdengine.batch_max_records > self.kafka.max_buffer_records:
            raise ValueError("batch exceeds maximum record buffer")
        if self.tdengine.batch_max_bytes > self.kafka.max_buffer_bytes:
            raise ValueError("batch exceeds maximum byte buffer")
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

    _UniqueLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
    )
    data = yaml.load(source.decode("utf-8"), Loader=_UniqueLoader)
    if not isinstance(data, dict):
        raise ValueError("Storage configuration must be a YAML mapping")
    return StorageConfig.model_validate(data)
