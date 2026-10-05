"""Commander 即时读取服务。"""

from __future__ import annotations

from wind_hub_commander.runtime import CommanderRuntime
from wind_hub_core.model.errors import CommandError
from wind_hub_core.model.point import PointRef, PointValue


class CommanderReadService:
    """按设备和 point_id 执行即时读取。"""

    def __init__(self, runtime: CommanderRuntime) -> None:
        self._runtime = runtime

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        """读取单点并返回工程值。"""
        values = await self.read_points(device_id, [point_id])
        return values[0]

    async def read_points(
        self,
        device_id: str,
        point_ids: list[str],
    ) -> list[PointValue]:
        """一次连接保证后批量读取多个点并返回工程值。"""
        async with self._runtime.operation():
            try:
                device = self._runtime.device(device_id)
            except KeyError as exc:
                raise CommandError(str(exc), "") from exc

            if not point_ids:
                raise CommandError("point_ids must be non-empty", "")

            known = {point.point_id for point in device.points}
            missing = [point_id for point_id in point_ids if point_id not in known]
            if missing:
                raise CommandError(
                    f"unknown points on device '{device_id}': {missing}",
                    "",
                )
            if not await self._runtime.ensure_connected(device_id):
                raise CommandError(f"device '{device_id}' is not connected", "")

            values = await device.read_points(
                [
                    PointRef(device_id=device_id, point_id=point_id)
                    for point_id in point_ids
                ]
            )
            by_id = {value.point_id: value for value in values}
            missing_values = [
                point_id for point_id in point_ids if point_id not in by_id
            ]
            if missing_values:
                raise CommandError(
                    f"device '{device_id}' returned no values for points {missing_values}",
                    "",
                )
            return [by_id[point_id] for point_id in point_ids]
