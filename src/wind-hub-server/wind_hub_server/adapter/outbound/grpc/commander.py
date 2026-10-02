"""Commander gRPC 出站适配器。

全部调用使用 commander.proto 生成的 Stub/Message；本模块只负责 Protobuf 与
Server 应用层既有 Python DTO/dict 边界转换。
"""

from __future__ import annotations

from typing import Any

from google.protobuf import empty_pb2

from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.point import PointValue
from wind_hub_core.rpc import commander_pb2 as pb
from wind_hub_core.rpc import commander_pb2_grpc as pb_grpc
from wind_hub_core.rpc.commander_io_codec import (
    command_result_from_proto,
    command_to_proto,
    decode_scalar,
    point_value_from_proto,
)
from wind_hub_server.adapter.outbound.grpc.common import GrpcClientBase


def _address_dict(items) -> dict[str, Any]:
    """把 AddressField 列表恢复为普通地址字典。"""
    return {item.key: decode_scalar(item.value) for item in items}


def _point_verify_dict(message: pb.PointVerifyResponse) -> dict[str, Any]:
    """把点诊断 wire message 转为 Server 现有字典边界。"""
    readable = message.readable.value if message.HasField("readable") else None
    raw_value = decode_scalar(message.raw_value) if message.HasField("raw_value") else None
    engineering_value = (
        decode_scalar(message.engineering_value)
        if message.HasField("engineering_value")
        else None
    )
    return {
        "device_id": message.device_id,
        "point_id": message.point_id,
        "variable_name": message.variable_name or None,
        "protocol": message.protocol,
        "configured_address": _address_dict(message.configured_address),
        "resolved_address": (
            _address_dict(message.resolved_address)
            if message.resolved_address
            else None
        ),
        "data_type": message.data_type,
        "scale": message.scale,
        "offset": message.offset,
        "unit": message.unit,
        "readable": readable,
        "ok": message.ok,
        "code": message.code,
        "severity": message.severity,
        "raw_value": raw_value,
        "engineering_value": engineering_value,
        "quality": message.quality or None,
        "source": message.source or None,
        "error": message.error or None,
    }


class CommanderGrpcClient(GrpcClientBase):
    """通过 generated CommanderServiceStub 调用独立 Commander。"""

    def __init__(self, target: str, *, default_timeout: float = 5.0) -> None:
        super().__init__(target, default_timeout=default_timeout)
        self._stub = pb_grpc.CommanderServiceStub(self._channel)

    async def status(self) -> dict[str, Any]:
        """返回 Commander 运行与配置状态。"""
        response = await self._stub.GetStatus(
            empty_pb2.Empty(),
            timeout=self.default_timeout,
        )
        return {
            "running": response.running,
            "device_count": response.device_count,
            "healthy_devices": response.healthy_devices,
            "active_revision": response.active_revision,
            "active_config_hash": response.active_config_hash,
            "prepared_revision": response.prepared_revision or None,
            "prepared_config_hash": response.prepared_config_hash or None,
        }

    async def reload_config(self) -> dict[str, Any]:
        """调用 Commander 兼容 ReloadConfig。"""
        response = await self._stub.ReloadConfig(
            empty_pb2.Empty(),
            timeout=30.0,
        )
        return {
            "success": response.success,
            "revision_id": response.revision_id,
            "active_config_hash": response.active_config_hash,
        }

    async def prepare_config(
        self,
        revision_id: str,
        config_hash: str,
    ) -> dict[str, Any]:
        """准备 Commander 指定配置 revision。"""
        response = await self._stub.PrepareConfig(
            pb.PrepareConfigRequest(
                revision_id=revision_id,
                config_hash=config_hash,
            ),
            timeout=30.0,
        )
        return {
            "success": response.success,
            "revision_id": response.revision_id,
            "config_hash": response.config_hash,
        }

    async def activate_config(self, revision_id: str) -> dict[str, Any]:
        """激活 Commander 指定 prepared revision。"""
        response = await self._stub.ActivateConfig(
            pb.ActivateConfigRequest(revision_id=revision_id),
            timeout=30.0,
        )
        return {
            "success": response.success,
            "revision_id": response.revision_id,
            "active_config_hash": response.active_config_hash,
        }

    async def abort_config(self, revision_id: str) -> dict[str, Any]:
        """撤销 Commander 指定 prepared revision。"""
        response = await self._stub.AbortConfig(
            pb.AbortConfigRequest(revision_id=revision_id),
            timeout=30.0,
        )
        return {
            "success": response.success,
            "revision_id": response.revision_id,
            "aborted": response.aborted,
        }

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        """即时读取单点。"""
        response = await self._stub.ReadPoint(
            pb.ReadPointRequest(device_id=device_id, point_id=point_id),
            timeout=self.default_timeout,
        )
        return point_value_from_proto(response)

    async def read_points(
        self,
        device_id: str,
        point_ids: list[str],
    ) -> list[PointValue]:
        """即时批量读取点位。"""
        response = await self._stub.ReadPoints(
            pb.ReadPointsRequest(device_id=device_id, point_ids=point_ids),
            timeout=self.default_timeout,
        )
        return [point_value_from_proto(item) for item in response.values]

    async def write(self, command: Command) -> CommandResult:
        """即时写入单点。"""
        response = await self._stub.WritePoint(
            command_to_proto(command),
            timeout=max(self.default_timeout, command.timeout + 1.0),
        )
        return command_result_from_proto(response)

    async def verify_device(
        self,
        device_id: str,
        timeout: float = 1.0,
    ) -> dict[str, Any]:
        """执行设备链路验证。"""
        response = await self._stub.VerifyDevice(
            pb.VerifyDeviceRequest(device_id=device_id, timeout=timeout),
            timeout=max(self.default_timeout, timeout + 1.0),
        )
        return {
            "device_id": response.device_id,
            "protocol": response.protocol,
            "host": response.host,
            "port": response.port,
            "ok": response.ok,
            "stages": [
                {
                    "name": stage.name,
                    "ok": stage.ok,
                    "code": stage.code,
                    "severity": stage.severity,
                    "message": stage.message,
                }
                for stage in response.stages
            ],
        }

    async def resolve_point(self, device_id: str, point_id: str) -> dict[str, Any]:
        """解析单点协议地址。"""
        response = await self._stub.ResolvePoint(
            pb.PointRequest(device_id=device_id, point_id=point_id),
            timeout=self.default_timeout,
        )
        return _point_verify_dict(response)

    async def verify_point(self, device_id: str, point_id: str) -> dict[str, Any]:
        """执行单点在线读取验证。"""
        response = await self._stub.VerifyPoint(
            pb.PointRequest(device_id=device_id, point_id=point_id),
            timeout=self.default_timeout,
        )
        return _point_verify_dict(response)

    async def verify_points(
        self,
        device_id: str,
        point_group: str | None = None,
    ) -> dict[str, Any]:
        """批量验证设备点表或指定 point_group。"""
        response = await self._stub.VerifyPoints(
            pb.VerifyPointsRequest(
                device_id=device_id,
                point_group=point_group or "",
            ),
            timeout=self.default_timeout,
        )
        return {
            "device_id": response.device_id,
            "point_group": response.point_group or None,
            "checked": response.checked,
            "passed": response.passed,
            "failed": response.failed,
            "ok": response.ok,
            "points": [_point_verify_dict(item) for item in response.points],
        }
