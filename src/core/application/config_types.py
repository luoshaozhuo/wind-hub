"""与配置源无关的类型化主题返回对象。

此处只定义数据契约。Schema 解析与具体配置格式由 Infrastructure 负责。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class DeviceTypeDefinition:
    name: str | None


@dataclass(frozen=True, slots=True)
class DeviceModelDefinition:
    device_type: str
    manufacturer: str | None
    model: str | None
    protocol: str
    point_table: str
    read_mode: str | None
    properties: Mapping[str, Any]
    connection_defaults: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DeviceModelsConfig:
    device_types: Mapping[str, DeviceTypeDefinition]
    device_models: Mapping[str, DeviceModelDefinition]


@dataclass(frozen=True, slots=True)
class EndpointDefinition:
    host: str
    port: int | None
    extensions: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DeviceInstanceDefinition:
    device_id: str
    model: str
    device_group: str | None
    endpoint: EndpointDefinition
    enabled: bool


@dataclass(frozen=True, slots=True)
class DeviceInstancesConfig:
    devices: tuple[DeviceInstanceDefinition, ...]


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    models: DeviceModelsConfig
    instances: DeviceInstancesConfig


@dataclass(frozen=True, slots=True)
class PointDefinition:
    point_id: str
    variable_name: str | None
    point_groups: tuple[str, ...]
    address: Mapping[str, Any]
    data_type: str
    scale: float
    offset: float
    unit: str
    description: str | None


@dataclass(frozen=True, slots=True)
class PointTableDefinition:
    protocol: str
    points: Mapping[str, PointDefinition]


@dataclass(frozen=True, slots=True)
class PointConfig:
    tables: Mapping[str, PointTableDefinition]


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_id: str
    device: str | None
    device_group: str | None
    point_group: str
    interval: float | None
    targets: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True, slots=True)
class TaskConfig:
    tasks: tuple[TaskDefinition, ...]


@dataclass(frozen=True, slots=True)
class SiteIdentity:
    """当前部署实例所属现场的身份（仅标识用途）。"""

    site_id: str
    name: str | None


@dataclass(frozen=True, slots=True)
class ADSLocalConfig:
    """进程级 ADS 本机身份与凭据（restart-required）。"""

    local_ams_net_id: str
    local_ip: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """跨进程共享的运行参数；``None`` 表示未配置，默认值由各进程自定。"""

    queue_maxsize: int | None = None
    backpressure_policy: str | None = None
    shutdown_timeout: float | None = None
    connect_timeout: float | None = None
    read_timeout: float | None = None
    write_timeout: float | None = None


@dataclass(frozen=True, slots=True)
class SystemConfig:
    """system.yaml 的共享段；进程专属段（如 interfaces）不进公共契约。"""

    site: SiteIdentity | None = None
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    ads: ADSLocalConfig | None = None


@dataclass(frozen=True, slots=True)
class UnitDefinition:
    symbol: str
    name: str | None


@dataclass(frozen=True, slots=True)
class UnitConfig:
    units: Mapping[str, UnitDefinition]
