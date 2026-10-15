"""V1 Device 查询服务。

静态设备定义来自 Server 当前配置快照；运行状态来自 MonitoringService 最近一次
低频 Collector 快照。页面读取不直接触发 Collector RPC。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.domain.device import Device
from web_server.application.config.service import ConfigService
from web_server.application.port.monitoring import (
    DeviceRuntimeSnapshot,
    MonitoringSnapshotPort,
)


class DeviceSnapshot(BaseModel):
    """设备静态配置与实时连接状态的聚合快照。"""

    device_id: str
    protocol: str
    host: str
    port: int | None
    point_table: str
    device_type: str | None = None
    model: str | None = None
    device_group: str | None = None
    extensions: dict[str, object] = Field(default_factory=dict)
    port_override: int | None = None
    extension_overrides: dict[str, object] = Field(default_factory=dict)
    enabled: bool
    connected: bool
    consecutive_failures: int = 0
    last_error: str | None = None


class DeviceQueryService:
    """设备管理页只读入口。"""

    def __init__(
        self,
        monitoring: MonitoringSnapshotPort,
        config: ConfigService,
    ) -> None:
        self._monitoring = monitoring
        self._config = config

    def list_devices(self, search: str | None = None) -> list[DeviceSnapshot]:
        """返回配置与最近一次 Collector 运行状态合并后的设备快照。"""
        runtime = {row.device_id: row for row in self._monitoring.devices_snapshot()}
        rows = [
            self._snapshot(cfg, runtime.get(cfg.device_id))
            for cfg in self._config.current_config.site.devices.values()
        ]
        query = (search or "").strip().lower()
        if query:
            rows = [
                row
                for row in rows
                if any(
                    query in str(value or "").lower()
                    for value in (
                        row.device_id,
                        row.host,
                        row.model,
                        row.device_type,
                        row.device_group,
                        row.protocol,
                    )
                )
            ]
        return sorted(rows, key=lambda row: row.device_id)

    def get_device(self, device_id: str) -> DeviceSnapshot:
        """返回单设备快照；未知设备抛 KeyError。"""
        cfg = next(
            (
                item
                for item in self._config.current_config.site.devices.values()
                if item.device_id == device_id
            ),
            None,
        )
        if cfg is None:
            raise KeyError(device_id)
        runtime = next(
            (row for row in self._monitoring.devices_snapshot() if row.device_id == device_id),
            None,
        )
        return self._snapshot(cfg, runtime)

    def _snapshot(
        self,
        cfg: Device,
        runtime: DeviceRuntimeSnapshot | None,
    ) -> DeviceSnapshot:
        snapshot = self._config.current_config
        model = snapshot.device_models[cfg.device_model_id]
        table = snapshot.point_tables[model.point_table_id]
        protocol_options = snapshot.protocol_options_by_device.get(cfg.device_id, {})
        defaults = model.endpoint_defaults
        port_override = cfg.endpoint.port if cfg.endpoint.port != defaults.get("port") else None
        return DeviceSnapshot(
            device_id=cfg.device_id,
            protocol=table.protocol.name,
            host=cfg.endpoint.host,
            port=cfg.endpoint.port,
            point_table=model.point_table_id,
            device_type=model.device_type_id,
            model=cfg.device_model_id,
            device_group=cfg.device_group_ids[0],
            extensions={**dict(model.protocol_options), **dict(protocol_options)},
            port_override=port_override,
            extension_overrides=dict(protocol_options),
            enabled=cfg.enabled,
            connected=runtime.connected if runtime is not None else False,
            consecutive_failures=(runtime.consecutive_failures if runtime is not None else 0),
            last_error=runtime.last_error if runtime is not None else None,
        )
