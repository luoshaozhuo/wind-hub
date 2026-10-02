"""Commander gRPC 出站适配器。"""

from __future__ import annotations

from typing import Any

from google.protobuf import empty_pb2

from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.point import PointValue
from wind_hub_core.rpc.commander import (
    GET_STATUS,
    READ_POINT,
    READ_POINTS,
    RESOLVE_POINT,
    VERIFY_DEVICE,
    VERIFY_POINT,
    VERIFY_POINTS,
    WRITE_POINT,
    rpc_path,
)
from wind_hub_server.adapter.outbound.grpc.common import (
    GrpcClientBase,
    from_struct,
    to_struct,
)


class CommanderGrpcClient(GrpcClientBase):
    """通过 gRPC 调用独立 Commander。"""

    async def status(self) -> dict[str, Any]:
        call = self.unary_empty_struct(rpc_path(GET_STATUS))
        return from_struct(await call(empty_pb2.Empty()))

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        call = self.unary_struct(rpc_path(READ_POINT))
        data = from_struct(
            await call(to_struct({"device_id": device_id, "point_id": point_id}))
        )
        return PointValue.model_validate(data)

    async def read_points(
        self,
        device_id: str,
        point_ids: list[str],
    ) -> list[PointValue]:
        call = self.unary_struct(rpc_path(READ_POINTS))
        data = from_struct(
            await call(
                to_struct(
                    {
                        "device_id": device_id,
                        "point_ids": point_ids,
                    }
                )
            )
        )
        return [
            PointValue.model_validate(item)
            for item in list(data.get("values") or [])
        ]

    async def write(self, command: Command) -> CommandResult:
        call = self.unary_struct(rpc_path(WRITE_POINT))
        data = from_struct(await call(to_struct(command.model_dump(mode="json"))))
        return CommandResult.model_validate(data)

    async def verify_device(
        self,
        device_id: str,
        timeout: float = 1.0,
    ) -> dict[str, Any]:
        call = self.unary_struct(rpc_path(VERIFY_DEVICE))
        return from_struct(
            await call(to_struct({"device_id": device_id, "timeout": timeout}))
        )

    async def resolve_point(self, device_id: str, point_id: str) -> dict[str, Any]:
        return await self._point_call(RESOLVE_POINT, device_id, point_id)

    async def verify_point(self, device_id: str, point_id: str) -> dict[str, Any]:
        return await self._point_call(VERIFY_POINT, device_id, point_id)

    async def verify_points(
        self,
        device_id: str,
        point_group: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"device_id": device_id}
        if point_group is not None:
            payload["point_group"] = point_group
        call = self.unary_struct(rpc_path(VERIFY_POINTS))
        return from_struct(await call(to_struct(payload)))

    async def _point_call(
        self,
        method: str,
        device_id: str,
        point_id: str,
    ) -> dict[str, Any]:
        call = self.unary_struct(rpc_path(method))
        return from_struct(
            await call(to_struct({"device_id": device_id, "point_id": point_id}))
        )
