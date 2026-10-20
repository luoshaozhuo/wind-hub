"""Gateway 配置：单文件 YAML，启动前严格验证。"""

from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import PointDefinition, PointRole
from .modbus_codec import RegisterMap, RegisterMapping
from .redis_mirror import RedisPointSource


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RedisSettings(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=6379, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    database: int = Field(default=0, ge=0, le=15)
    timeout: float = Field(default=1.0, gt=0)
    refresh_seconds: float = Field(default=0.4, gt=0)


class CommanderSettings(StrictModel):
    target: str = "127.0.0.1:50052"
    timeout: float = Field(default=1.0, gt=0)


class PointSettings(StrictModel):
    id: str = Field(min_length=1)
    role: PointRole
    writable: bool = False
    area: str
    address: int = Field(ge=0, le=65535)
    data_type: str
    word_order: str = "big_endian"
    redis_device_id: str | None = None
    redis_point_id: str | None = None
    key_prefix: str | None = None
    max_age_seconds: float = Field(default=3.0, gt=0)
    critical: bool = False
    commander_device_id: str | None = None
    commander_point_id: str | None = None
    commander_timeout: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def validate_routes(self) -> PointSettings:
        if self.role is PointRole.FEEDBACK:
            if not self.redis_device_id or not self.redis_point_id or not self.key_prefix:
                raise ValueError("feedback point requires Redis source and key_prefix")
            if self.commander_device_id or self.commander_point_id:
                raise ValueError("feedback point cannot have Commander target")
        elif self.role is PointRole.COMMAND:
            if not self.writable or not self.commander_device_id or not self.commander_point_id:
                raise ValueError("command point requires writable and Commander target")
            if self.redis_point_id or self.redis_device_id:
                raise ValueError("command point cannot have Redis source")
        elif self.commander_device_id or self.commander_point_id or self.redis_point_id:
            raise ValueError("parameter point must not have external routes")
        return self


class DeviceSettings(StrictModel):
    id: str = Field(min_length=1)
    host: str
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=1, le=247)
    points: list[PointSettings] = Field(min_length=1)


class GatewaySettings(StrictModel):
    redis: RedisSettings = RedisSettings()
    commander: CommanderSettings = CommanderSettings()
    devices: list[DeviceSettings] = Field(min_length=1)
    control_enabled: bool = False
    control_journal: Path | None = None
    control_client_ips: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_all(self) -> GatewaySettings:
        if len(set(self.control_client_ips)) != len(self.control_client_ips):
            raise ValueError("duplicate control client IP")
        for address in self.control_client_ips:
            ip_address(address)
        if self.control_enabled and (
            not self.control_client_ips or self.control_journal is None
        ):
            raise ValueError("control requires client allowlist and durable journal path")
        ids: set[str] = set()
        control_targets: set[tuple[str, str]] = set()
        endpoints: set[tuple[str, int]] = set()
        for device in self.devices:
            if device.id in ids:
                raise ValueError(f"duplicate device {device.id}")
            ids.add(device.id)
            endpoint = (device.host, device.port)
            if endpoint in endpoints:
                raise ValueError(f"duplicate listen endpoint {endpoint}")
            endpoints.add(endpoint)
            point_ids: set[str] = set()
            mappings: list[RegisterMapping] = []
            for point in device.points:
                if point.id in point_ids:
                    raise ValueError(f"duplicate point {device.id}.{point.id}")
                point_ids.add(point.id)
                PointDefinition(device.id, point.id, point.role, point.writable)
                if point.critical and point.role is not PointRole.FEEDBACK:
                    raise ValueError("critical requires feedback role")
                mappings.append(RegisterMapping(
                    device.id, point.id, point.area, point.address,
                    point.data_type, point.word_order
                ))
                if point.role is PointRole.COMMAND:
                    target = (point.commander_device_id or "", point.commander_point_id or "")
                    if target in control_targets:
                        raise ValueError(f"duplicate physical command target: {target}")
                    control_targets.add(target)
                if point.role is PointRole.COMMAND and point.area not in {"holding", "coil"}:
                    raise ValueError("command must use writable Modbus area")
                if point.role is PointRole.FEEDBACK and point.writable:
                    raise ValueError("Redis-owned feedback must not be client-writable")
                if point.writable and point.area not in {"holding", "coil"}:
                    raise ValueError("writable point must use holding or coil")
            RegisterMap(mappings)
        return self


def load_config(path: str | Path) -> GatewaySettings:
    """解析配置并拒绝未知字段、非法映射和跨设备监听冲突。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return GatewaySettings.model_validate(data)
