"""风电场领域模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .device import Device
from .identities import DeviceId


@dataclass(frozen=True, slots=True)
class Site:
    """现场风电场：唯一身份、名称及所属设备。"""

    site_id: str
    name: str
    devices: Mapping[DeviceId, Device]

    def __post_init__(self) -> None:
        if not isinstance(self.site_id, str) or not self.site_id.strip():
            raise ValueError("site_id must be non-empty")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("site name must be non-empty")
        devices = dict(self.devices)
        for device_id, device in devices.items():
            if device_id != device.device_id:
                raise ValueError(
                    f"site device key '{device_id}' does not match '{device.device_id}'"
                )
        object.__setattr__(self, "site_id", self.site_id.strip())
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "devices", MappingProxyType(devices))
