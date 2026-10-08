"""配置读取端口：按配置主题解耦，供上层组合所需配置子集。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

ConfigSection = Mapping[str, Any]


class SystemConfigReader(Protocol):
    def read_system(self) -> ConfigSection: ...


class DeviceConfigReader(Protocol):
    def read_devices(self) -> ConfigSection: ...


class DeviceModelConfigReader(Protocol):
    def read_device_models(self) -> ConfigSection: ...


class PointConfigReader(Protocol):
    def read_points(self) -> ConfigSection: ...


class UnitConfigReader(Protocol):
    def read_units(self) -> ConfigSection: ...


class TaskConfigReader(Protocol):
    def read_tasks(self) -> ConfigSection: ...


class SinkConfigReader(Protocol):
    def read_sinks(self) -> ConfigSection: ...


class ConfigFingerprintReader(Protocol):
    def fingerprint(self) -> str: ...


class DeviceDefinitionReader(
    DeviceConfigReader,
    DeviceModelConfigReader,
    PointConfigReader,
    UnitConfigReader,
    Protocol,
):
    """设备配置所需的最小组合端口。"""


@runtime_checkable
class CollectorConfigReader(
    DeviceDefinitionReader,
    SystemConfigReader,
    TaskConfigReader,
    SinkConfigReader,
    Protocol,
):
    """Collector 配置加载所需端口。"""


@runtime_checkable
class CommanderConfigReader(
    DeviceDefinitionReader,
    SystemConfigReader,
    Protocol,
):
    """Commander 配置加载所需端口。"""
