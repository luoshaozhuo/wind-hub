"""Industrial Gateway 共享配置：端点、虚拟设备与点表相互独立。"""

from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from industrial_gateway.infrastructure.protocol.modbus.codec import RegisterMap, RegisterMapping


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Endpoint(_Strict):
    protocol: Literal["modbus"]
    host: str = "127.0.0.1"
    port: int = Field(ge=1, le=65535)
    protocol_options: dict[str, str] = Field(default_factory=dict)


class Address(_Strict):
    register_type: Literal["coil", "discrete_input", "holding", "input"]
    address: int = Field(ge=0, le=65535)
    word_order: Literal["big_endian", "little_endian"] = "big_endian"


class Point(_Strict):
    role: Literal["feedback", "parameter", "command"]
    data_type: Literal[
        "bool", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64"
    ]
    address: Address
    max_age_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_modbus_point(self) -> Point:
        mapping = RegisterMapping(
            "validation_device",
            "validation_point",
            self.address.register_type,
            self.address.address,
            self.data_type,
            self.address.word_order,
        )
        if mapping.area not in {"coil", "holding"}:
            raise ValueError("writable points must use coil or holding registers")
        if self.role == "command" and self.max_age_seconds is not None:
            raise ValueError("command points cannot have feedback expiry")
        return self


class PointTable(_Strict):
    protocol: Literal["modbus"]
    points: dict[str, Point] = Field(min_length=1)


class CommandTarget(_Strict):
    device_id: str = Field(min_length=1)
    point_id: str = Field(min_length=1)


class DeviceAddress(_Strict):
    unit_id: int = Field(ge=1, le=247)


class Device(_Strict):
    endpoint_id: str
    point_table_id: str
    protocol_address: DeviceAddress
    commands: dict[str, CommandTarget] = Field(default_factory=dict)


class CommanderConfig(_Strict):
    endpoint: str
    command_timeout: float = Field(default=3.0, gt=0)
    rpc_timeout: float = Field(default=4.0, gt=0)

    @model_validator(mode="after")
    def check_timeout(self) -> CommanderConfig:
        if self.rpc_timeout <= self.command_timeout:
            raise ValueError("rpc_timeout must exceed command_timeout")
        return self


class Access(_Strict):
    data_writers: list[str] = Field(default_factory=list)
    command_writers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_addresses(self) -> Access:
        for address in self.data_writers + self.command_writers:
            ip_address(address)
        if set(self.data_writers) & set(self.command_writers):
            raise ValueError("data and command writers must be disjoint")
        return self


class Control(_Strict):
    enabled: bool = False
    commander: CommanderConfig
    journal_path: Path
    access: Access = Access()

    @model_validator(mode="after")
    def check_authorization(self) -> Control:
        if self.enabled and not self.access.command_writers:
            raise ValueError("enabled control needs explicit command writers")
        return self


class GatewayConfig(_Strict):
    endpoints: dict[str, Endpoint] = Field(min_length=1)
    point_tables: dict[str, PointTable] = Field(min_length=1)
    devices: dict[str, Device] = Field(min_length=1)
    control: Control

    @model_validator(mode="after")
    def validate_references(self) -> GatewayConfig:
        endpoints: set[tuple[str, int]] = set()
        units: set[tuple[str, int]] = set()
        physical_targets: set[tuple[str, str]] = set()
        for endpoint in self.endpoints.values():
            if endpoint.protocol_options not in ({}, {"mode": "tcp"}):
                raise ValueError("unsupported Modbus endpoint options")
            key = (endpoint.host, endpoint.port)
            if key in endpoints:
                raise ValueError("duplicate listen endpoint")
            endpoints.add(key)
        for device_id, device in self.devices.items():
            if (
                device.endpoint_id not in self.endpoints
                or device.point_table_id not in self.point_tables
            ):
                raise ValueError(f"undefined endpoint or point table for {device_id}")
            endpoint = self.endpoints[device.endpoint_id]
            table = self.point_tables[device.point_table_id]
            if endpoint.protocol != table.protocol:
                raise ValueError(f"protocol mismatch for {device_id}")
            unit = (device.endpoint_id, device.protocol_address.unit_id)
            if unit in units:
                raise ValueError(f"duplicate unit ID under endpoint: {unit}")
            units.add(unit)
            mappings: list[RegisterMapping] = []
            for point_id, point in table.points.items():
                mappings.append(
                    RegisterMapping(
                        device_id,
                        point_id,
                        point.address.register_type,
                        point.address.address,
                        point.data_type,
                        point.address.word_order,
                    )
                )
            RegisterMap(mappings)
            for point_id, point in table.points.items():
                if point.role == "command" and point_id not in device.commands:
                    raise ValueError(f"missing command target: {device_id}.{point_id}")
                if point.role != "command" and point_id in device.commands:
                    raise ValueError(f"non-command point has command target: {point_id}")
            for point_id, target in device.commands.items():
                if point_id not in table.points:
                    raise ValueError(f"unknown command point: {point_id}")
                target_key = (target.device_id, target.point_id)
                if target_key in physical_targets:
                    raise ValueError(f"duplicate physical target: {target_key}")
                physical_targets.add(target_key)
        return self


def load_config(path: str | Path) -> GatewayConfig:
    """从 configs 目录加载，不接受未知的旧 Redis 配置字段。"""
    return GatewayConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
