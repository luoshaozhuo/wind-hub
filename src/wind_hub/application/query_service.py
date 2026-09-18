"""Query service — 只读查询的应用服务。

将协议驱动与设备配置包装为
:class:`~wind_hub.domain.port.inbound.QueryUseCase`：单点实时读（绕过采集循环
直接调用协议驱动的 ``read``）、设备列表与单设备信息。

``read_point`` 的错误语义对齐端口契约：
- 设备不存在 → :class:`CommandError`（API 层映射为 404）。
- 点不存在 → :class:`CommandError`（同样 404）。
- 协议读失败 → :class:`ProtocolError` 原样上抛（API 层映射为 503）。
"""

from __future__ import annotations

from wind_hub.config.schema import DeviceConfig, PointConfig
from wind_hub.domain.engine.scheduler import Scheduler
from wind_hub.domain.model.device import DeviceInfo
from wind_hub.domain.model.errors import CommandError, ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue
from wind_hub.domain.port.inbound import QueryUseCase
from wind_hub.domain.port.outbound import ProtocolPort


class QueryService(QueryUseCase):
    """只读查询服务。

    ``points``（按 device_id 分组的点表）用于在实时读之前判定点是否存在，
    从而把「未知点」与「读失败」区分开，给调用方稳定的 404 / 503 语义。
    """

    def __init__(
        self,
        scheduler: Scheduler,
        protocols: dict[str, ProtocolPort],
        devices: dict[str, DeviceConfig],
        points: dict[str, list[PointConfig]] | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._protocols = protocols
        self._devices = devices
        self._points = points or {}

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        """实时读取单个点，绕过采集缓存直接走协议驱动。

        Raises:
            CommandError: 设备或点未知。
            ProtocolError: 协议驱动读失败（设备不可达等）。
        """
        if device_id not in self._devices:
            raise CommandError(f"unknown device '{device_id}'", "")

        device_points = self._points.get(device_id, [])
        if not any(p.point_id == point_id for p in device_points):
            raise CommandError(f"unknown point '{device_id}/{point_id}'", "")

        proto = self._protocols.get(device_id)
        if proto is None:
            raise CommandError(f"no protocol driver for device '{device_id}'", "")

        values = await proto.read([PointRef(device_id=device_id, point_id=point_id)])
        if not values:
            raise ProtocolError(f"read returned no value for '{device_id}/{point_id}'")
        return values[0]

    async def list_devices(self) -> list[DeviceInfo]:
        """返回所有配置设备的运行时状态。"""
        return [self._device_info(device_id, cfg) for device_id, cfg in self._devices.items()]

    async def get_device_info(self, device_id: str) -> DeviceInfo:
        """返回单设备运行时状态。

        Raises:
            CommandError: 设备未知。
        """
        cfg = self._devices.get(device_id)
        if cfg is None:
            raise CommandError(f"unknown device '{device_id}'", "")
        return self._device_info(device_id, cfg)

    def _device_info(self, device_id: str, cfg: DeviceConfig) -> DeviceInfo:
        """从设备配置 + 协议健康状态构造 :class:`DeviceInfo`。

        ``last_seen`` 暂无逐设备读取时间戳追踪，恒为 ``None``（诚实空缺，
        待后续步骤在采集循环中补齐）。
        """
        proto = self._protocols.get(device_id)
        connected = proto.health().healthy if proto is not None else False
        return DeviceInfo(
            device_id=device_id,
            protocol=cfg.protocol,
            connected=connected,
        )
