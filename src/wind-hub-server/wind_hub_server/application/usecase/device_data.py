"""Devices Data / Trend 页的缓存查询用例。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from wind_hub_server.application.port.point_store import LatestPointStore, TrendStore
from wind_hub_server.application.usecase.config import ConfigUseCase
from wind_hub_core.model.point import PointValue, Quality


class DeviceDataItem(BaseModel):
    """点定义 metadata 与最近采集值的合并快照。"""

    point_id: str
    variable_name: str | None = None
    point_groups: list[str]
    data_type: str
    unit: str
    unit_symbol: str
    description: str | None = None
    value: Any = None
    quality: Quality | None = None
    timestamp: datetime | None = None
    source: str | None = None


class TrendSeries(BaseModel):
    """单点短期趋势序列。"""

    point_id: str
    variable_name: str | None = None
    unit: str
    unit_symbol: str
    samples: list[PointValue]


class DeviceDataUseCase:
    """只读缓存查询；不会因为 Data/Trend 页面刷新而主动访问 PLC。"""

    def __init__(
        self,
        config: ConfigUseCase,
        latest: LatestPointStore,
        trend: TrendStore,
    ) -> None:
        self._config = config
        self._latest = latest
        self._trend = trend

    async def list_data(
        self,
        device_id: str,
        *,
        search: str | None = None,
        point_group: str | None = None,
    ) -> list[DeviceDataItem]:
        """返回当前点表全部点，并合并缓存中的最近值。"""
        points = self._points_or_raise(device_id)
        latest = self._latest.list_device(device_id)
        query = (search or "").strip().lower()
        rows: list[DeviceDataItem] = []
        for point in points:
            if point_group and point_group not in point.point_groups:
                continue
            if query and not any(
                query in str(value or "").lower()
                for value in (point.point_id, point.variable_name, point.description)
            ):
                continue
            value = latest.get(point.point_id)
            rows.append(
                DeviceDataItem(
                    point_id=point.point_id,
                    variable_name=point.variable_name,
                    point_groups=list(point.point_groups),
                    data_type=point.data_type,
                    unit=point.unit,
                    unit_symbol=self._unit_symbol(point.unit),
                    description=point.description,
                    value=value.value if value is not None else None,
                    quality=value.quality if value is not None else None,
                    timestamp=value.timestamp if value is not None else None,
                    source=value.source if value is not None else None,
                )
            )
        return rows

    async def trend(
        self,
        device_id: str,
        point_ids: list[str],
        *,
        window_seconds: int = 600,
        limit_per_point: int = 600,
    ) -> list[TrendSeries]:
        """查询指定点的短期内存趋势。"""
        points = self._points_or_raise(device_id)
        definitions = {point.point_id: point for point in points}
        requested = list(dict.fromkeys(point_ids))
        unknown = [point_id for point_id in requested if point_id not in definitions]
        if unknown:
            raise KeyError(f"unknown points: {', '.join(unknown)}")
        since = datetime.now(UTC) - timedelta(seconds=window_seconds)
        values = self._trend.query(
            device_id,
            set(requested),
            since=since,
            limit_per_point=limit_per_point,
        )
        return [
            TrendSeries(
                point_id=point_id,
                variable_name=definitions[point_id].variable_name,
                unit=definitions[point_id].unit,
                unit_symbol=self._unit_symbol(definitions[point_id].unit),
                samples=values.get(point_id, []),
            )
            for point_id in requested
        ]

    def _points_or_raise(self, device_id: str):
        """从当前配置快照返回设备绑定的 resolved 点表。"""
        cfg = self._config.current_config
        device = next(
            (item for item in cfg.devices.devices if item.device_id == device_id),
            None,
        )
        if device is None:
            raise KeyError(device_id)
        return list(cfg.point_tables.tables[device.point_table].points)

    def _unit_symbol(self, unit_id: str) -> str:
        """由当前配置快照解析单位显示符号。"""
        unit = self._config.current_config.units.units.get(unit_id)
        return unit.symbol if unit is not None else unit_id
