"""Strict Industrial Gateway v1 service configuration (no shared device copies)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstanceSources(StrictModel):
    devices: Path

    @field_validator("devices", mode="before")
    @classmethod
    def require_pathname(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("config_sources.instances.devices requires a pathname")
        return value


class TemplateSources(StrictModel):
    root: Path

    @field_validator("root", mode="before")
    @classmethod
    def require_pathname(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("config_sources.templates.root requires a pathname")
        return value


class ConfigSources(StrictModel):
    instances: InstanceSources
    templates: TemplateSources


class TlsConfig(StrictModel):
    enabled: bool = False
    cert_file: Path | None = None
    key_file: Path | None = None
    ca_file: Path | None = None

    @model_validator(mode="after")
    def validate_certificates(self) -> TlsConfig:
        if self.enabled:
            paths = (self.cert_file, self.key_file, self.ca_file)
            if any(path is None or str(path) in {"", "."} for path in paths):
                raise ValueError("enabled mTLS requires explicit certificate file paths")
        return self


class GrpcConfig(StrictModel):
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=50073, ge=1, le=65535)
    max_active_rpcs: int = Field(default=64, ge=1)
    request_max_bytes: int = Field(default=2097152, ge=1)
    tls: TlsConfig = Field(default_factory=TlsConfig)
    permissions: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_listener(self) -> GrpcConfig:
        if not self.listen_host.strip():
            raise ValueError("grpc.listen_host cannot be empty")
        allowed = {
            "read",
            "config_read",
            "config_write",
            "inject",
            "lifecycle",
            "diagnose",
        }
        for name, rights in self.permissions.items():
            if not name.strip() or not rights or not set(rights) <= allowed:
                raise ValueError("invalid grpc identity permissions")
        if self.tls.enabled and not self.permissions:
            raise ValueError("production mTLS requires explicit identity permissions")
        if not self.tls.enabled and self.listen_host not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("non-loopback gRPC requires mTLS")
        return self


class CommandMapping(StrictModel):
    variable_id: str = Field(min_length=1)
    commander_point_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_nonblank(self) -> CommandMapping:
        if not self.variable_id.strip() or not self.commander_point_id.strip():
            raise ValueError("control mapping identifiers cannot be blank")
        return self


class ServerConfig(StrictModel):
    server_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    listen_host: str = Field(min_length=1)
    listen_port: int = Field(ge=1, le=65535)
    desired_state: Literal["START", "STOP"] = "STOP"
    control_enabled: bool = False
    command_mappings: tuple[CommandMapping, ...] = ()

    @model_validator(mode="after")
    def validate_mapping_ids(self) -> ServerConfig:
        if any(not item.strip() for item in (self.server_id, self.device_id, self.listen_host)):
            raise ValueError("server_id, device_id and listen_host cannot be blank")
        ids = [entry.variable_id for entry in self.command_mappings]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate variable_id in command_mappings")
        return self


class ControlConfig(StrictModel):
    commander_endpoint: str = Field(min_length=1)
    tls: TlsConfig = Field(default_factory=TlsConfig)
    forward_queue_capacity: int = Field(default=1024, ge=1, le=65536)
    max_forward_inflight: int = Field(default=16, ge=1, le=256)
    submit_deadline_ms: int = Field(default=5000, ge=1, le=60000)


class MetricsConfig(StrictModel):
    window_seconds: int = Field(default=60, ge=1, le=3600)
    collect_interval_ms: int = Field(default=1000, ge=100, le=60000)


class GatewayServiceConfig(StrictModel):
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    file_version: str
    config_sources: ConfigSources
    grpc: GrpcConfig = Field(default_factory=GrpcConfig)
    servers: tuple[ServerConfig, ...] = Field(min_length=1)
    control: ControlConfig
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)

    @model_validator(mode="after")
    def validate_service(self) -> GatewayServiceConfig:
        if not _VERSION.fullmatch(self.file_version):
            raise ValueError("invalid service file_version")
        if self.metrics.window_seconds * 1000 < self.metrics.collect_interval_ms:
            raise ValueError("metrics window must cover at least one collection interval")
        if not self.control.commander_endpoint.strip():
            raise ValueError("control.commander_endpoint cannot be blank")
        endpoint = self.control.commander_endpoint
        host = endpoint.rsplit(":", 1)[0].strip("[]")
        if not self.control.tls.enabled and host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("non-loopback Commander requires mTLS")
        identifiers: set[str] = set()
        devices: set[str] = set()
        listeners: list[tuple[str, int]] = [(self.grpc.listen_host, self.grpc.listen_port)]
        for server in self.servers:
            if server.server_id in identifiers:
                raise ValueError(f"duplicate server_id: {server.server_id}")
            if server.device_id in devices:
                raise ValueError(f"duplicate server device_id: {server.device_id}")
            identifiers.add(server.server_id)
            devices.add(server.device_id)
            for host, port in listeners:
                previous_host = host.casefold()
                current_host = server.listen_host.casefold()
                loopback = {"localhost", "127.0.0.1", "::1"}
                overlaps = (
                    previous_host == current_host
                    or previous_host in {"0.0.0.0", "::"}
                    or current_host in {"0.0.0.0", "::"}
                    or (previous_host in loopback and current_host in loopback)
                )
                if port == server.listen_port and overlaps:
                    raise ValueError(f"overlapping listen port: {server.listen_port}")
            listeners.append((server.listen_host, server.listen_port))
        return self


def service_path(root: Path, pathname: Path) -> Path:
    """Resolve a configured pathname against an explicit deployment root."""
    base = root.resolve(strict=True)
    candidate = (pathname if pathname.is_absolute() else base / pathname).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError(f"configuration pathname escapes deployment root: {pathname}")
    return candidate
