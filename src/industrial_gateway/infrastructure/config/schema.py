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


class TargetMapping(StrictModel):
    variable_id: str = Field(min_length=1)
    target_point_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identifiers(self) -> TargetMapping:
        if not self.variable_id.strip() or not self.target_point_id.strip():
            raise ValueError("control mapping identifiers must not be blank")
        return self


class GenerationBinding(StrictModel):
    scenario_id: str = Field(min_length=1)

    @field_validator("scenario_id")
    @classmethod
    def validate_scenario(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Generator scenario binding must not be blank")
        return value


class ControlTarget(StrictModel):
    kind: Literal["commander", "data_generator", "none"]
    endpoint: str | None = None
    tls: TlsConfig | None = None
    request_timeout_ms: int = Field(default=5000, ge=1, le=60000)
    queue_capacity: int = Field(default=1024, ge=1, le=65536)
    max_inflight: int = Field(default=1, ge=1, le=256)
    generation_binding: GenerationBinding | None = None

    @model_validator(mode="after")
    def validate_target(self) -> ControlTarget:
        if self.kind == "none":
            if (
                self.endpoint is not None
                or self.tls is not None
                or self.generation_binding is not None
            ):
                raise ValueError("none control target cannot have an external endpoint")
            return self
        endpoint = self.endpoint
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("control target endpoint must be configured")
        host, separator, port_text = endpoint.rpartition(":")
        if (
            not separator
            or not host.strip()
            or not port_text.isdecimal()
            or not 1 <= int(port_text) <= 65535
        ):
            raise ValueError("control target endpoint must be host:port")
        host = host.strip("[]")
        if (not self.tls or not self.tls.enabled) and host not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("non-loopback control target requires mTLS")
        if self.kind == "data_generator":
            if self.generation_binding is None:
                raise ValueError("Generator target requires a Scenario binding")
            if self.tls is None or not self.tls.enabled:
                raise ValueError("Generator model update requires mutual TLS")
        elif self.generation_binding is not None:
            raise ValueError("only Generator targets may bind a Scenario")
        return self


class ServerControl(StrictModel):
    enabled: bool
    target: ControlTarget
    mappings: tuple[TargetMapping, ...] = ()

    @model_validator(mode="after")
    def validate_control(self) -> ServerControl:
        variables = [mapping.variable_id for mapping in self.mappings]
        if len(variables) != len(set(variables)):
            raise ValueError("duplicate control mapping variable_id")
        if self.enabled:
            if self.target.kind == "none" or not variables:
                raise ValueError("enabled control requires a target and mappings")
        elif self.mappings or self.target.kind != "none":
            raise ValueError("disabled control cannot declare active mappings or target")
        return self


class ServerConfig(StrictModel):
    server_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    listen_host: str = Field(min_length=1)
    listen_port: int = Field(ge=1, le=65535)
    desired_state: Literal["START", "STOP"] = "STOP"
    control: ServerControl

    @model_validator(mode="after")
    def validate_server(self) -> ServerConfig:
        if any(not item.strip() for item in (self.server_id, self.device_id, self.listen_host)):
            raise ValueError("Server identifiers and host must not be blank")
        return self


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
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)

    @model_validator(mode="after")
    def validate_service(self) -> GatewayServiceConfig:
        if not _VERSION.fullmatch(self.file_version):
            raise ValueError("invalid service file_version")
        if self.metrics.window_seconds * 1000 < self.metrics.collect_interval_ms:
            raise ValueError("metrics window must cover at least one collection interval")
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
