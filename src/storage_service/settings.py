"""Storage bootstrap: only connection identities and persistent cache paths."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfigurationEndpoint(_Settings):
    endpoint: str = Field(min_length=1)
    token_env: str = Field(min_length=1)

    @field_validator("token_env")
    @classmethod
    def validate_token_env(cls, value: str) -> str:
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", value) is None:
            raise ValueError("token environment variable must be a safe name")
        return value


class LocalCache(_Settings):
    directory: Path

    @field_validator("directory")
    @classmethod
    def validate_directory(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("Storage cache must be an absolute, non-traversing path")
        return value


class NatsEndpoint(_Settings):
    servers: list[str] = Field(min_length=1)

    @field_validator("servers")
    @classmethod
    def validate_servers(cls, value: list[str]) -> list[str]:
        if any(
            not isinstance(item, str) or not item.startswith("nats://") or len(item) > 512
            for item in value
        ):
            raise ValueError("NATS server URLs must use explicit nats:// addresses")
        return value


class StorageBootstrap(_Settings):
    """No business Kafka/TDengine options: those must come from Configuration."""

    schema_version: Literal[1]
    file_version: str
    service_id: str = Field(min_length=1, max_length=256)
    configuration: ConfigurationEndpoint
    nats: NatsEndpoint
    cache: LocalCache

    @field_validator("file_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", value) is None:
            raise ValueError("bootstrap file_version must be MAJOR.MINOR.PATCH")
        return value


def load_bootstrap(path: Path) -> StorageBootstrap:
    if not path.is_file():
        raise ValueError("Storage bootstrap YAML file is missing")
    if path.stat().st_size > 65536:
        raise ValueError("Storage bootstrap exceeds 64 KiB")
    data = yaml.safe_load(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError("Storage bootstrap must be a mapping")
    return StorageBootstrap.model_validate(data)
