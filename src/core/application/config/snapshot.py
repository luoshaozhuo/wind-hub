"""Shared Core 静态配置快照。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TypeVar

from core.domain import (
    BusinessPoint,
    BusinessPointId,
    ConnectionId,
    Device,
    DeviceGroup,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    DeviceType,
    DeviceTypeId,
    PointTable,
    PointTableId,
)

from .device_connection import DeviceConnection

_KeyT = TypeVar("_KeyT")
_ValueT = TypeVar("_ValueT")


def _freeze_index(
    values: Mapping[_KeyT, _ValueT],
) -> Mapping[_KeyT, _ValueT]:
    """返回索引的只读浅拷贝。"""
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class CoreConfigSnapshot:
    """Collector、Commander、Server 共享的不可变静态配置。

    仅保存跨应用共享的设备、业务点、点表和通信接入定义。
    CollectionTask、PointSet、Sink 等应用专有配置不得进入本快照。
    """

    device_types: Mapping[DeviceTypeId, DeviceType] = field(default_factory=dict)
    device_models: Mapping[DeviceModelId, DeviceModel] = field(default_factory=dict)
    device_groups: Mapping[DeviceGroupId, DeviceGroup] = field(default_factory=dict)
    devices: Mapping[DeviceId, Device] = field(default_factory=dict)
    business_points: Mapping[BusinessPointId, BusinessPoint] = field(default_factory=dict)
    point_tables: Mapping[PointTableId, PointTable] = field(default_factory=dict)
    device_connections: Mapping[ConnectionId, DeviceConnection] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        fields = {
            "device_types": _freeze_index(self.device_types),
            "device_models": _freeze_index(self.device_models),
            "device_groups": _freeze_index(self.device_groups),
            "devices": _freeze_index(self.devices),
            "business_points": _freeze_index(self.business_points),
            "point_tables": _freeze_index(self.point_tables),
            "device_connections": _freeze_index(self.device_connections),
        }

        self._validate_identity(fields["device_types"], "device_types", "device_type_id")
        self._validate_identity(fields["device_models"], "device_models", "device_model_id")
        self._validate_identity(fields["device_groups"], "device_groups", "device_group_id")
        self._validate_identity(fields["devices"], "devices", "device_id")
        self._validate_identity(
            fields["business_points"],
            "business_points",
            "business_point_id",
        )
        self._validate_identity(fields["point_tables"], "point_tables", "point_table_id")
        self._validate_identity(
            fields["device_connections"],
            "device_connections",
            "connection_id",
        )

        for name, value in fields.items():
            object.__setattr__(self, name, value)

    @staticmethod
    def _validate_identity(
        values: Mapping[object, object],
        index_name: str,
        identity_attr: str,
    ) -> None:
        """校验索引键与对象内部稳定身份一致。"""
        for key, value in values.items():
            identity = getattr(value, identity_attr)
            if key != identity:
                raise ValueError(
                    f"{index_name} key '{key}' does not match "
                    f"{identity_attr} '{identity}'"
                )
