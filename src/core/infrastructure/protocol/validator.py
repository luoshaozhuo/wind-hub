"""基于显式 ProtocolRegistry 的协议配置静态校验。"""

from __future__ import annotations

from core.application.config import CoreConfigSnapshot
from core.application.port import CoreConfigValidatorPort

from .registry import ProtocolRegistry


class ProtocolConfigValidator(CoreConfigValidatorPort):
    """通过创建未连接 Driver 对协议专有配置做 fail-fast 校验。

    Driver factory 必须只解析配置和预编译点表，不得在 create 阶段执行网络 I/O。
    """

    def __init__(self, protocols: ProtocolRegistry) -> None:
        self._protocols = protocols

    def validate(self, snapshot: CoreConfigSnapshot) -> None:
        """校验全部 DeviceConnection 的协议专有连接参数与点地址。"""
        for connection in snapshot.device_connections.values():
            point_table = snapshot.point_table_for_device(connection.device_id)
            self._protocols.create(
                connection,
                point_table,
                snapshot.connection_options_for(
                    connection.connection_id
                ),
                snapshot.point_options.get(
                    point_table.point_table_id,
                    {},
                ),
            )
