"""配置读取端口：按配置主题解耦，供上层组合所需配置子集。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

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
