"""V1 Device 查询用例。

把 Runtime 中的 Device 配置、协议健康状态和重连状态聚合为稳定展示模型；
不负责 Device CRUD、Verify 或直接点读取。配置修改仍属于 ConfigUseCase。
"""

from __future__ import annotations

from pydantic import BaseModel

from wind_hub.application.runtime.runtime import Runtime
from wind_hub.config.schema import DeviceConfig
from wind_hub.application.usecase.config import ConfigUseCase


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
    extensions: dict[str, object]
    port_override: int | None
    extension_overrides: dict[str, object]
    enabled: bool
    connected: bool
    consecutive_failures: int = 0
    last_error: str | None = None


class DeviceUseCase:
    """设备管理页的只读查询入口。"""

    def __init__(
        self,
        runtime: Runtime,
        config: ConfigUseCase | None = None,
    ) -> None:
        self._runtime = runtime
        self._config = config

    async def list_devices(self, search: str | None = None) -> list[DeviceSnapshot]:
        """返回设备快照，可按 ID/地址/型号/分组进行大小写无关过滤。"""
        rows = [self._snapshot(device_id) for device_id in self._runtime.devices]
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
        """返回单设备快照；未知 ID 抛 KeyError。"""
        if device_id not in self._runtime.devices:
            raise KeyError(device_id)
        return self._snapshot(device_id)

    def _snapshot(self, device_id: str) -> DeviceSnapshot:
        """从 Runtime 当前对象构造快照，不缓存热重载前的旧配置。"""
        device = self._runtime.devices[device_id]
        cfg = device.config
        state = self._runtime.device_state(device_id)
        port_override, extension_overrides = self._connection_overrides(cfg)
        return DeviceSnapshot(
            device_id=device_id,
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
            connected=device.health().healthy,
            consecutive_failures=state.consecutive_failures if state is not None else 0,
            last_error=state.last_error if state is not None else None,
        )

    def _connection_overrides(
        self,
        cfg: DeviceConfig,
    ) -> tuple[int | None, dict[str, object]]:
        """从 resolved endpoint 反推出实例连接差异，避免把型号默认值固化。"""
        endpoint = cfg.endpoint
        if self._config is None or not cfg.model:
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
