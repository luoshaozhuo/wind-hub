"""Commander gRPC 出站适配器。"""

from __future__ import annotations

from typing import Any

from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.point import PointValue
from wind_hub_core.rpc import commander_io_pb2 as io_pb
from wind_hub_core.rpc.commander_io_codec import (
    command_result_from_proto,
    command_to_proto,
    point_value_from_proto,
)
from wind_hub_core.rpc.commander import (
    ACTIVATE_CONFIG,
    ABORT_CONFIG,
    GET_STATUS,
    PREPARE_CONFIG,
    READ_POINT,
    READ_POINTS,
    RELOAD_CONFIG,
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
        return from_struct(
            await self.call_empty_struct(rpc_path(GET_STATUS))
        )

    async def reload_config(self) -> dict[str, Any]:
        return from_struct(
            await self.call_empty_struct(
                rpc_path(RELOAD_CONFIG),
                timeout=30.0,
            )
        )

    async def prepare_config(
        self,
        revision_id: str,
        config_hash: str,
    ) -> dict[str, Any]:
        return from_struct(
            await self.call_struct(
                rpc_path(PREPARE_CONFIG),
                to_struct(
                    {
                        "revision_id": revision_id,
                        "config_hash": config_hash,
                    }
                ),
                timeout=30.0,
            )
        )

    async def activate_config(self, revision_id: str) -> dict[str, Any]:
        return from_struct(
            await self.call_struct(
                rpc_path(ACTIVATE_CONFIG),
                to_struct({"revision_id": revision_id}),
                timeout=30.0,
            )
        )

    async def abort_config(self, revision_id: str) -> dict[str, Any]:
        """撤销 Commander 指定 prepared revision。"""
        return from_struct(
            await self.call_struct(
                rpc_path(ABORT_CONFIG),
                to_struct({"revision_id": revision_id}),
                timeout=30.0,
            )
        )

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        call = self._channel.unary_unary(
            rpc_path(READ_POINT),
            request_serializer=io_pb.ReadPointRequest.SerializeToString,
            response_deserializer=io_pb.PointValueMessage.FromString,
        )
        response = await call(
            io_pb.ReadPointRequest(device_id=device_id, point_id=point_id),
            timeout=self.default_timeout,
        )
        return point_value_from_proto(response)

    async def read_points(
        self,
        device_id: str,
        point_ids: list[str],
    ) -> list[PointValue]:
        call = self._channel.unary_unary(
            rpc_path(READ_POINTS),
            request_serializer=io_pb.ReadPointsRequest.SerializeToString,
            response_deserializer=io_pb.ReadPointsResponse.FromString,
        )
        response = await call(
            io_pb.ReadPointsRequest(
                device_id=device_id,
                point_ids=point_ids,
            ),
            timeout=self.default_timeout,
        )
        return [point_value_from_proto(item) for item in response.values]

    async def write(self, command: Command) -> CommandResult:
        call = self._channel.unary_unary(
            rpc_path(WRITE_POINT),
            request_serializer=io_pb.WritePointRequest.SerializeToString,
            response_deserializer=io_pb.CommandResultMessage.FromString,
        )
        response = await call(
            command_to_proto(command),
            timeout=max(self.default_timeout, command.timeout + 1.0),
        )
        return command_result_from_proto(response)

    async def verify_device(
        self,
        device_id: str,
        timeout: float = 1.0,
    ) -> dict[str, Any]:
        return from_struct(
            await self.call_struct(
                rpc_path(VERIFY_DEVICE),
                to_struct({"device_id": device_id, "timeout": timeout}),
                timeout=max(self.default_timeout, timeout + 1.0),
            )
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
        return from_struct(
            await self.call_struct(
                rpc_path(VERIFY_POINTS),
                to_struct(payload),
            )
        )

    async def _point_call(
        self,
        method: str,
        device_id: str,
        point_id: str,
    ) -> dict[str, Any]:
        return from_struct(
            await self.call_struct(
                rpc_path(method),
                to_struct({"device_id": device_id, "point_id": point_id}),
            )
        )
