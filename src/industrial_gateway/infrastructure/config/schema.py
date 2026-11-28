"""Strict Industrial Gateway service configuration without shared device copies."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstanceSources(StrictModel):
    devices: str

    @field_validator("devices", mode="before")
    @classmethod
    def require_pathname(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("config_sources.instances.devices requires a pathname")
        return value


class ConfigSources(StrictModel):
    instances: InstanceSources


class SecurityCaller(StrictModel):
    caller_id: str = Field(min_length=1)
    token_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")
    permissions: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_caller(self) -> SecurityCaller:
        allowed = {
            "read",
            "config_read",
            "config_write",
            "inject",
            "simulation_execute",
            "lifecycle",
            "diagnose",
        }
        if not self.caller_id.strip():
            raise ValueError("caller_id cannot be blank")
        if (
            len(self.permissions) != len(set(self.permissions))
            or not set(self.permissions) <= allowed
        ):
            raise ValueError("invalid caller permissions")
        return self


class SecurityConfig(StrictModel):
    callers: tuple[SecurityCaller, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_callers(self) -> SecurityConfig:
        ids = [caller.caller_id for caller in self.callers]
        references = [caller.token_env for caller in self.callers]
        if len(ids) != len(set(ids)) or len(references) != len(set(references)):
            raise ValueError("duplicate security caller identity or token reference")
        return self


class GrpcConfig(StrictModel):
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=50073, ge=1, le=65535)
    sink_listener_port: int | None = Field(default=None, ge=1, le=65535)
    max_active_rpcs: int = Field(default=64, ge=1)
    request_max_bytes: int = Field(default=2097152, ge=1)

    @model_validator(mode="after")
    def validate_listener(self) -> GrpcConfig:
        if self.listen_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Gateway gRPC must bind loopback")
        if self.sink_listener_port is not None and self.sink_listener_port == self.listen_port:
            raise ValueError("Gateway Sink listener must differ from management listener")
        return self


class TargetMapping(StrictModel):
    variable_id: str = Field(min_length=1)
    target_point_id: str = Field(min_length=1)
    # Convert protocol engineering units into Commander target units on forwarding.
    # The local Modbus register retains the original wire value.
    value_scale: FiniteFloat = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def validate_identifiers(self) -> TargetMapping:
        if not self.variable_id.strip() or not self.target_point_id.strip():
            raise ValueError("control mapping identifiers must not be blank")
        return self


class SimulationTarget(StrictModel):
    """Generator input binding allowed only after Commander authorization."""

    endpoint: str = Field(min_length=1)
    token_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")
    scenario_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_simulation(self) -> SimulationTarget:
        if not self.scenario_id.strip():
            raise ValueError("simulation Scenario must not be blank")
        host, separator, port_text = self.endpoint.rpartition(":")
        if (
            not separator
            or host not in {"127.0.0.1", "localhost", "::1"}
            or not port_text.isdecimal()
            or not 1 <= int(port_text) <= 65535
        ):
            raise ValueError("Generator endpoint must be loopback host:port")
        return self


class ControlTarget(StrictModel):
    kind: Literal["commander", "none"]
    endpoint: str | None = None
    token_env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")
    request_timeout_ms: int = Field(default=5000, ge=1, le=60000)
    queue_capacity: int = Field(default=1024, ge=1, le=65536)
    max_inflight: int = Field(default=1, ge=1, le=256)

    @model_validator(mode="after")
    def validate_target(self) -> ControlTarget:
        if self.kind == "none":
            if self.endpoint is not None or self.token_env is not None:
                raise ValueError("none control target cannot declare an endpoint or token")
            return self
        endpoint = self.endpoint
        if not isinstance(endpoint, str) or not self.token_env:
            raise ValueError("Commander target requires endpoint and token_env")
        host, separator, port_text = endpoint.rpartition(":")
        if (
            not separator
            or host not in {"127.0.0.1", "localhost", "::1"}
            or not port_text.isdecimal()
            or not 1 <= int(port_text) <= 65535
        ):
            raise ValueError("Commander endpoint must be loopback host:port")
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
    read_stale_after_ms: int | None = Field(default=None, ge=1)
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
    service_id: str = Field(min_length=1)
    schema_version: Literal[2, 3]

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value not in {2, 3}:
            raise ValueError("schema_version must be integer 2 or 3")
        return value

    file_version: str
    config_sources: ConfigSources
    security: SecurityConfig
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
        if self.grpc.sink_listener_port is not None:
            listeners.append(("127.0.0.1", self.grpc.sink_listener_port))
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
