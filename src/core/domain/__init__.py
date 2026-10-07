"""Shared Core Domain 公共门面。"""

from .config import (
    CoreConfigDiff,
    CoreConfigSnapshot,
    IndexDiff,
    ProtocolOptions,
    ProtocolOptionValue,
    compute_core_config_diff,
    freeze_protocol_options,
    validate_core_config,
)
from .device import Device, DeviceGroup, DeviceModel, DeviceType
from .identities import (
    BusinessPointId,
    DeviceGroupId,
    DeviceId,
    DeviceModelId,
    DeviceTypeId,
    PointTableId,
)
from .point import BusinessPoint, Point, PointTable
from .unit import *
from .value_objects import ConnectionEndpoint, DataType, PointAccess, Protocol

__all__ = [
    "BusinessPoint",
    "BusinessPointId",
    "ConnectionEndpoint",
    "CoreConfigDiff",
    "CoreConfigSnapshot",
    "DataType",
    "Device",
    "DeviceGroup",
    "DeviceGroupId",
    "DeviceId",
    "DeviceModel",
    "DeviceModelId",
    "DeviceType",
    "DeviceTypeId",
    "IndexDiff",
    "Point",
    "PointAccess",
    "PointTable",
    "PointTableId",
    "Protocol",
    "ProtocolOptionValue",
    "ProtocolOptions",
    "compute_core_config_diff",
    "freeze_protocol_options",
    "validate_core_config",
]
