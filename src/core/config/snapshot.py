"""共享静态配置快照。

ConfigSnapshot 只表达已经构造完成的共享静态配置视图。它负责保存不可变索引，
并保证索引键与对象自身稳定身份一致；跨对象引用完整性由 core.validation 负责。

本模块不负责 YAML/JSON 解析、文件 I/O、环境变量、热重载、运行时实例创建或
协议连接建立。
"""

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
from .identities import PointSetId, SinkId, TaskId
from .point_set import PointSet
from .sink import SinkDefinition
from .task import CollectionTask

_KeyT = TypeVar("_KeyT")
_ValueT = TypeVar("_ValueT")


def _freeze_index(
    values: Mapping[_KeyT, _ValueT],
) -> Mapping[_KeyT, _ValueT]:
    """返回索引的只读浅拷贝。"""
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """wind-hub 共享静态配置的不可变快照。

    所有集合均按对象稳定身份建立索引。Snapshot 只承担静态数据组织，不执行
    resolve、I/O 或 Runtime orchestration。
    """

    device_types: Mapping[DeviceTypeId, DeviceType] = field(default_factory=dict)
    device_models: Mapping[DeviceModelId, DeviceModel] = field(default_factory=dict)
    device_groups: Mapping[DeviceGroupId, DeviceGroup] = field(default_factory=dict)
    devices: Mapping[DeviceId, Device] = field(default_factory=dict)
    business_points: Mapping[BusinessPointId, BusinessPoint] = field(default_factory=dict)
    point_tables: Mapping[PointTableId, PointTable] = field(default_factory=dict)
    point_sets: Mapping[PointSetId, PointSet] = field(default_factory=dict)
    tasks: Mapping[TaskId, CollectionTask] = field(default_factory=dict)
    sinks: Mapping[SinkId, SinkDefinition] = field(default_factory=dict)
    device_connections: Mapping[ConnectionId, DeviceConnection] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        device_types = _freeze_index(self.device_types)
        device_models = _freeze_index(self.device_models)
        device_groups = _freeze_index(self.device_groups)
        devices = _freeze_index(self.devices)
        business_points = _freeze_index(self.business_points)
        point_tables = _freeze_index(self.point_tables)
        point_sets = _freeze_index(self.point_sets)
        tasks = _freeze_index(self.tasks)
        sinks = _freeze_index(self.sinks)
        device_connections = _freeze_index(self.device_connections)

        self._validate_identity(device_types, "device_types", "device_type_id")
        self._validate_identity(device_models, "device_models", "device_model_id")
        self._validate_identity(device_groups, "device_groups", "device_group_id")
        self._validate_identity(devices, "devices", "device_id")
        self._validate_identity(
            business_points,
            "business_points",
            "business_point_id",
        )
        self._validate_identity(point_tables, "point_tables", "point_table_id")
        self._validate_identity(point_sets, "point_sets", "point_set_id")
        self._validate_identity(tasks, "tasks", "task_id")
        self._validate_identity(sinks, "sinks", "sink_id")
        self._validate_identity(
            device_connections,
            "device_connections",
            "connection_id",
        )

        object.__setattr__(self, "device_types", device_types)
        object.__setattr__(self, "device_models", device_models)
        object.__setattr__(self, "device_groups", device_groups)
        object.__setattr__(self, "devices", devices)
        object.__setattr__(self, "business_points", business_points)
        object.__setattr__(self, "point_tables", point_tables)
        object.__setattr__(self, "point_sets", point_sets)
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "sinks", sinks)
        object.__setattr__(self, "device_connections", device_connections)

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
