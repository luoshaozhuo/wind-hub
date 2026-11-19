"""Strict Industrial Gateway service configuration without shared device copies."""

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


class GrpcConfig(StrictModel):
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=50073, ge=1, le=65535)
    max_active_rpcs: int = Field(default=64, ge=1)
    request_max_bytes: int = Field(default=2097152, ge=1)
    token_sources: dict[str, str]
    permissions: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def validate_listener(self) -> GrpcConfig:
        if not self.listen_host.strip():
            raise ValueError("grpc.listen_host cannot be empty")
        allowed = {
            "read", "config_read", "config_write", "inject",
            "simulation_execute", "lifecycle", "diagnose",
        }
        if not self.token_sources or set(self.token_sources) != set(self.permissions):
            raise ValueError("Gateway token_sources and permissions must match")
        for name, rights in self.permissions.items():
            if not name.strip() or not rights or not set(rights) <= allowed:
                raise ValueError("invalid Gateway permissions")
        for name in self.token_sources.values():
            if re.fullmatch(r"env:[A-Z_][A-Z0-9_]*", name) is None:
                raise ValueError("Gateway token_sources values must reference env:NAME")
        return self


class TargetMapping(StrictModel):
    variable_id: str = Field(min_length=1)
    target_point_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identifiers(self) -> TargetMapping:
        if not self.variable_id.strip() or not self.target_point_id.strip():
            raise ValueError("control mapping identifiers must not be blank")
        return self


class SimulationTarget(StrictModel):
    """Authenticated Generator endpoint used after Commander authorization."""

    endpoint: str = Field(min_length=1)
    token_env: str
    scenario_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_simulation(self) -> SimulationTarget:
        if not self.scenario_id.strip() or not self.endpoint.strip():
            raise ValueError("Generator endpoint and scenario are required")
        if re.fullmatch(r"env:[A-Z_][A-Z0-9_]*", self.token_env) is None:
            raise ValueError("Generator token_env must reference env:NAME")
        return self


class ControlTarget(StrictModel):
    kind: Literal["commander", "none"]
    endpoint: str | None = None
    token_env: str | None = None
    request_timeout_ms: int = Field(default=5000, ge=1, le=60000)
    queue_capacity: int = Field(default=1024, ge=1, le=65536)
    max_inflight: int = Field(default=1, ge=1, le=256)

    @model_validator(mode="after")
    def validate_target(self) -> ControlTarget:
        if self.kind == "none":
            if self.endpoint is not None or self.token_env is not None:
                raise ValueError("none control target cannot declare an endpoint")
            return self
        if not self.endpoint or not self.endpoint.strip():
            raise ValueError("Commander control target requires endpoint")
        if not self.token_env or re.fullmatch(
            r"env:[A-Z_][A-Z0-9_]*", self.token_env
        ) is None:
            raise ValueError("Commander target requires env:NAME token")
        return self


class ServerControl(StrictModel):
    enabled: bool
    target: ControlTarget
    mappings: tuple[TargetMapping, ...] = ()
    simulation: SimulationTarget | None = None

    @model_validator(mode="after")
    def validate_control(self) -> ServerControl:
        variables = [mapping.variable_id for mapping in self.mappings]
        if len(variables) != len(set(variables)):
            raise ValueError("duplicate control mapping variable_id")
        if self.enabled:
            if self.target.kind == "none" or not variables:
                raise ValueError("enabled control requires a target and mappings")
        elif self.mappings or self.target.kind != "none" or self.simulation is not None:
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
