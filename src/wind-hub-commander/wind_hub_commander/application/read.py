"""Commander 即时读取用例。"""

from __future__ import annotations

from wind_hub_commander.runtime import CommanderRuntime
from wind_hub_core.model.errors import CommandError
from wind_hub_core.model.point import PointRef, PointValue


class ReadUseCase:
    """按设备和 point_id 执行即时读取。"""

    def __init__(self, runtime: CommanderRuntime) -> None:
        self._runtime = runtime

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        """读取单点并返回工程值。"""
        try:
            device = self._runtime.device(device_id)
        except KeyError as exc:
            raise CommandError(str(exc), "") from exc

        if not any(point.point_id == point_id for point in device.points):
            raise CommandError(f"unknown point '{device_id}/{point_id}'", "")
        if not await self._runtime.ensure_connected(device_id):
            raise CommandError(f"device '{device_id}' is not connected", "")

        values = await device.read_points(
            [PointRef(device_id=device_id, point_id=point_id)]
        )
        if not values:
            raise CommandError(
                f"device '{device_id}' returned no value for point '{point_id}'",
                "",
            )
        return values[0]
