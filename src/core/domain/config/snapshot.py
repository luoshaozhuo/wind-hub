"""Shared Domain 静态配置快照。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Any, TypeAlias, TypeVar

from ..device import Device, DeviceGroup, DeviceModel, DeviceType
from ..identities import (
    BusinessPointId,
    DeviceGroupId,
    DeviceId,
    DeviceModelId,
    DeviceTypeId,
    PointTableId,
)
from ..point import BusinessPoint, PointTable

ProtocolOptionValue: TypeAlias = str | int | float | bool | None
ProtocolOptions: TypeAlias = Mapping[str, ProtocolOptionValue]

_KeyT = TypeVar("_KeyT")
_ValueT = TypeVar("_ValueT")


def _freeze_index(
    values: Mapping[_KeyT, _ValueT],
) -> Mapping[_KeyT, _ValueT]:
    return MappingProxyType(dict(values))


def _freeze_options(
    values: Mapping[str, ProtocolOptionValue],
) -> ProtocolOptions:
    options = dict(values)
    for key, value in options.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("protocol option keys must be non-empty strings")
        if isinstance(value, float) and not isfinite(value):
            raise ValueError(f"protocol option '{key}' must be finite")
    return MappingProxyType(options)


@dataclass(frozen=True, slots=True)
class CoreConfigSnapshot:
    """Collector、Commander、Server 共享的不可变领域配置快照。"""

    device_types: Mapping[DeviceTypeId, DeviceType] = field(default_factory=dict)
    device_models: Mapping[DeviceModelId, DeviceModel] = field(default_factory=dict)
    device_groups: Mapping[DeviceGroupId, DeviceGroup] = field(default_factory=dict)
    devices: Mapping[DeviceId, Device] = field(default_factory=dict)
    business_points: Mapping[BusinessPointId, BusinessPoint] = field(default_factory=dict)
    point_tables: Mapping[PointTableId, PointTable] = field(default_factory=dict)
    device_options: Mapping[DeviceId, ProtocolOptions] = field(default_factory=dict)

    def __post_init__(self) -> None:
        fields: dict[str, Mapping[Any, Any]] = {
            "device_types": _freeze_index(self.device_types),
            "device_models": _freeze_index(self.device_models),
            "device_groups": _freeze_index(self.device_groups),
            "devices": _freeze_index(self.devices),
            "business_points": _freeze_index(self.business_points),
            "point_tables": _freeze_index(self.point_tables),
            "device_options": MappingProxyType(
                {
                    device_id: _freeze_options(options)
                    for device_id, options in self.device_options.items()
                }
            ),
        }

        self._validate_identity(
            fields["device_types"],
            "device_types",
            "device_type_id",
        )
        self._validate_identity(
            fields["device_models"],
            "device_models",
            "device_model_id",
        )
        self._validate_identity(
            fields["device_groups"],
            "device_groups",
            "device_group_id",
        )
        self._validate_identity(fields["devices"], "devices", "device_id")
        self._validate_identity(
            fields["business_points"],
            "business_points",
            "business_point_id",
        )
        self._validate_identity(
            fields["point_tables"],
            "point_tables",
            "point_table_id",
        )
        for name, value in fields.items():
            object.__setattr__(self, name, value)

    @staticmethod
    def _validate_identity(
        values: Mapping[object, object],
        index_name: str,
        identity_attr: str,
    ) -> None:
        for key, value in values.items():
            identity = getattr(value, identity_attr)
            if key != identity:
                raise ValueError(
                    f"{index_name} key '{key}' does not match "
                    f"{identity_attr} '{identity}'"
                )

    def point_table_for_device(self, device_id: DeviceId) -> PointTable:
        """解析 Device -> DeviceModel -> PointTable。"""
        device = self.devices[device_id]
        model = self.device_models[device.device_model_id]
        return self.point_tables[model.point_table_id]

    def device_options_for(
        self,
        device_id: DeviceId,
    ) -> ProtocolOptions:
        """返回指定 Device 的协议专有连接配置。"""
        return self.device_options.get(device_id, MappingProxyType({}))
