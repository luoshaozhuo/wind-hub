"""协议专有配置静态校验。"""

from __future__ import annotations

from core.domain import CoreConfigSnapshot

from .registry import ProtocolRegistry


class ProtocolConfigValidator:
    """通过创建未连接 Driver 对协议专有配置做 fail-fast 校验。"""

    def __init__(self, protocols: ProtocolRegistry) -> None:
        self._protocols = protocols

    def validate(self, snapshot: CoreConfigSnapshot) -> None:
        for device in snapshot.devices.values():
            point_table = snapshot.point_table_for_device(device.device_id)
            self._protocols.create(
                device.endpoint,
                point_table,
                snapshot.device_options_for(device.device_id),
            )
