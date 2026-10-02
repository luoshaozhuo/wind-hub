"""V1 Device 查询用例。

静态设备定义来自 Server 当前配置快照；实时连接状态来自 Collector gRPC。
Server 不直接读取 Collector Runtime 对象。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from wind_hub_core.config.schema import DeviceConfig
from wind_hub_server.application.port.collector_query import CollectorQueryPort
from wind_hub_server.application.usecase.config import ConfigUseCase


class DeviceSnapshot(BaseModel):
    """设备静态配置与实时连接状态的聚合快照。"""

    device_id: str
    protocol: str
    host: str
    port: int
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


class DeviceUseCase:
    """设备管理页只读入口。"""

    def __init__(
        self,
        collectors: CollectorQueryPort,
        config: ConfigUseCase,
    ) -> None:
        self._collectors = collectors
        self._config = config

    async def list_devices(self, search: str | None = None) -> list[DeviceSnapshot]:
        """返回配置与 Collector 当前连接状态合并后的设备快照。"""
        runtime_rows = await self._collectors.list_devices()
        runtime = {
            str(row.get("device_id")): row
            for row in runtime_rows
            if row.get("device_id") is not None
        }
        rows = [
            self._snapshot(cfg, runtime.get(cfg.device_id))
            for cfg in self._config.current_config.devices.devices
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

    async def get_device(self, device_id: str) -> DeviceSnapshot:
        """返回单设备快照；未知设备抛 KeyError。"""
        cfg = next(
            (
                item
                for item in self._config.current_config.devices.devices
                if item.device_id == device_id
            ),
            None,
        )
        if cfg is None:
            raise KeyError(device_id)
        runtime_rows = await self._collectors.list_devices()
        runtime = next(
            (
                row
                for row in runtime_rows
                if str(row.get("device_id")) == device_id
            ),
            None,
        )
        return self._snapshot(cfg, runtime)

    def _snapshot(
        self,
        cfg: DeviceConfig,
        runtime: dict[str, Any] | None,
    ) -> DeviceSnapshot:
        port_override, extension_overrides = self._connection_overrides(cfg)
        return DeviceSnapshot(
            device_id=cfg.device_id,
            protocol=cfg.protocol,
            host=cfg.endpoint.host,
            port=cfg.endpoint.port,
            point_table=cfg.point_table,
            device_type=cfg.device_type,
            model=cfg.model,
            device_group=cfg.device_group,
            extensions=dict(cfg.endpoint.extensions),
            port_override=port_override,
            extension_overrides=extension_overrides,
            enabled=cfg.enabled,
            connected=bool(runtime.get("connected")) if runtime is not None else False,
            consecutive_failures=(
                int(runtime.get("consecutive_failures") or 0)
                if runtime is not None
                else 0
            ),
            last_error=(
                str(runtime.get("last_error"))
                if runtime is not None and runtime.get("last_error")
                else None
            ),
        )

    def _connection_overrides(
        self,
        cfg: DeviceConfig,
    ) -> tuple[int | None, dict[str, object]]:
        """从 resolved endpoint 反推出实例连接差异。"""
        endpoint = cfg.endpoint
        if not cfg.model:
            return endpoint.port, dict(endpoint.extensions)
        model = self._config.current_config.device_models.get(cfg.model)
        if model is None:
            return endpoint.port, dict(endpoint.extensions)
        defaults = dict(model.connection_defaults)
        default_port = defaults.pop("port", None)
        port_override = (
            endpoint.port
            if default_port is None or endpoint.port != default_port
            else None
        )
        extensions = {
            key: value
            for key, value in endpoint.extensions.items()
            if key not in defaults or defaults[key] != value
        }
        return port_override, extensions
