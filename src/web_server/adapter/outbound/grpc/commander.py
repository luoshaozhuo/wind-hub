"""Commander gRPC 出站适配器。

全部调用使用 commander.proto 生成的 Stub/Message；本模块只负责 Protobuf 与
Server 应用层 DTO 边界转换。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC
from typing import Any

from google.protobuf import empty_pb2

from core.application.command import Command, CommandResult
from core.application.protocol_contract import WriteOutcome
from core.domain.sample_value import Quality
from core.infrastructure.rpc import commander_pb2 as pb
from core.infrastructure.rpc import commander_pb2_grpc as pb_grpc
from web_server.adapter.outbound.grpc.common import GrpcClientBase
from web_server.application.device.sample import PointValue
from web_server.application.port.worker import (
    CommanderStatus,
    ConfigAbortAck,
    ConfigActivateAck,
    ConfigPrepareAck,
    DeviceVerifyResult,
    DiagnosticStage,
    PointVerifyResult,
)


def _decode_scalar(value: pb.ScalarValue) -> Any:
    """解码 protobuf oneof 标量，避免经过 JSON 中间结构。"""
    kind = value.WhichOneof("kind")
    if kind is None:
        raise ValueError("Commander scalar has no active value")
    if kind == "bytes_value":
        raise ValueError("byte-valued Commander scalar is not supported by the Server")
    return getattr(value, kind)


def _encode_scalar(value: bool | int | float | str) -> pb.ScalarValue:
    """严格编码设备写入标量；不将 bool 误判为 int。"""
    result = pb.ScalarValue()
    if isinstance(value, bool):
        result.bool_value = value
    elif isinstance(value, int):
        if -(2**63) <= value < 2**63:
            result.sint64_value = value
        elif 0 <= value < 2**64:
            result.uint64_value = value
        else:
            raise ValueError("write integer outside 64-bit protobuf range")
    elif isinstance(value, float):
        result.double_value = value
    elif isinstance(value, str):
        result.string_value = value
    else:
        raise TypeError(f"unsupported write value: {type(value).__name__}")
    return result


def _point_value(message: pb.PointValueMessage) -> PointValue:
    """将 Commander 读取结果转换为 Server 采样 DTO。"""
    if not message.HasField("timestamp"):
        raise ValueError("Commander point value has no timestamp")
    if not message.HasField("value"):
        raise ValueError("Commander point value has no scalar")
    return PointValue(
        device_id=message.device_id,
        point_id=message.point_id,
        value=_decode_scalar(message.value),
        quality=Quality(message.quality),
        timestamp=message.timestamp.ToDatetime(tzinfo=UTC),
        source=message.source or None,
        timestamp_source=message.timestamp_source or "local",
    )


def _command_result(message: pb.CommandResultMessage) -> CommandResult:
    """保留设备写入的三态结果，UNKNOWN 禁止重发。"""
    outcomes = {
        pb.COMMAND_OUTCOME_CONFIRMED_SUCCESS: WriteOutcome.CONFIRMED_SUCCESS,
        pb.COMMAND_OUTCOME_CONFIRMED_FAILURE: WriteOutcome.CONFIRMED_FAILURE,
        pb.COMMAND_OUTCOME_UNKNOWN: WriteOutcome.UNKNOWN,
    }
    if message.outcome not in outcomes:
        raise ValueError(f"unsupported command outcome: {message.outcome}")
    if not message.HasField("finished_at"):
        raise ValueError("Commander command result has no finish timestamp")
    if not message.command_id:
        raise ValueError("Commander command result has no command ID")
    return CommandResult(
        command_id=message.command_id,
        outcome=outcomes[message.outcome],
        error=message.error or None,
        finished_at=message.finished_at.ToDatetime(tzinfo=UTC),
    )


def _address_dict(items: Iterable[pb.AddressField]) -> dict[str, Any]:
    """把 AddressField 列表恢复为普通地址字典。"""
    return {item.key: _decode_scalar(item.value) for item in items}


def _point_verify_dto(message: pb.PointVerifyResponse) -> PointVerifyResult:
    """把点诊断 wire message 转为 Server 应用层 DTO。"""
    readable = message.readable.value if message.HasField("readable") else None
    raw_value = _decode_scalar(message.raw_value) if message.HasField("raw_value") else None
    engineering_value = (
        _decode_scalar(message.engineering_value) if message.HasField("engineering_value") else None
    )
    return PointVerifyResult(
        device_id=message.device_id,
        point_id=message.point_id,
        variable_name=None,
        protocol=message.protocol,
        configured_address=_address_dict(message.configured_address),
        resolved_address=(
            _address_dict(message.resolved_address) if message.resolved_address else None
        ),
        data_type=message.data_type,
        scale=message.scale,
        offset=message.offset,
        unit=message.unit,
        readable=readable,
        ok=message.ok,
        code=message.code,
        severity=message.severity,
        raw_value=raw_value,
        engineering_value=engineering_value,
        quality=message.quality or None,
        source=message.source or None,
        error=message.error or None,
    )


class CommanderGrpcClient(GrpcClientBase):
    """通过 generated CommanderServiceStub 调用独立 Commander。"""

    def __init__(self, target: str, *, default_timeout: float = 5.0) -> None:
        super().__init__(target, default_timeout=default_timeout)
        self._stub = pb_grpc.CommanderServiceStub(self._channel)

    async def status(self) -> CommanderStatus:
        """返回 Commander 运行与配置状态。"""
        response = await self._stub.GetStatus(
            empty_pb2.Empty(),
            timeout=self.default_timeout,
        )
        return CommanderStatus(
            running=response.running,
            device_count=response.device_count,
            active_revision=response.active_revision,
            active_config_hash=response.active_config_hash,
            prepared_revision=response.prepared_revision or None,
        )

    async def prepare_config(
        self,
        revision_id: str,
        config_hash: str,
    ) -> ConfigPrepareAck:
        """准备 Commander 指定配置 revision。"""
        response = await self._stub.PrepareConfig(
            pb.PrepareConfigRequest(
                revision_id=revision_id,
                config_hash=config_hash,
            ),
            timeout=30.0,
        )
        return ConfigPrepareAck(
            success=response.success,
            revision_id=response.revision_id,
            config_hash=response.config_hash,
        )

    async def activate_config(self, revision_id: str) -> ConfigActivateAck:
        """激活 Commander 指定 prepared revision。"""
        response = await self._stub.ActivateConfig(
            pb.ActivateConfigRequest(revision_id=revision_id),
            timeout=30.0,
        )
        return ConfigActivateAck(
            success=response.success,
            revision_id=response.revision_id,
            active_config_hash=response.active_config_hash,
        )

    async def abort_config(self, revision_id: str) -> ConfigAbortAck:
        """撤销 Commander 指定 prepared revision。"""
        response = await self._stub.AbortConfig(
            pb.AbortConfigRequest(revision_id=revision_id),
            timeout=30.0,
        )
        return ConfigAbortAck(
            success=response.success,
            revision_id=response.revision_id,
            aborted=response.aborted,
        )

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        """即时读取单点。"""
        response = await self._stub.ReadPoint(
            pb.ReadPointRequest(device_id=device_id, point_id=point_id),
            timeout=self.default_timeout,
        )
        return _point_value(response)

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
        return [_point_value(item) for item in response.values]

    async def write(self, command: Command) -> CommandResult:
        """即时写入单点。"""
        response = await self._stub.WritePoint(
            pb.WritePointRequest(
                command_id=command.command_id,
                device_id=command.device_id,
                point_id=command.point_id,
                value=_encode_scalar(command.value),
                timeout=command.timeout,
            ),
            timeout=max(self.default_timeout, command.timeout + 1.0),
        )
        return _command_result(response)

    async def verify_device(
        self,
        device_id: str,
        timeout: float = 1.0,
    ) -> DeviceVerifyResult:
        """执行设备链路验证。"""
        response = await self._stub.VerifyDevice(
            pb.VerifyDeviceRequest(device_id=device_id, timeout=timeout),
            timeout=max(self.default_timeout, timeout + 1.0),
        )
        return DeviceVerifyResult(
            device_id=response.device_id,
            protocol=response.protocol,
            host=response.host,
            port=response.port,
            ok=response.ok,
            stages=[
                DiagnosticStage(
                    name=stage.name,
                    ok=stage.ok,
                    code=stage.code,
                    severity=stage.severity,
                    message=stage.message,
                )
                for stage in response.stages
            ],
        )

    async def verify_point(self, device_id: str, point_id: str) -> PointVerifyResult:
        """执行单点在线读取验证。"""
        response = await self._stub.VerifyPoint(
            pb.PointRequest(device_id=device_id, point_id=point_id),
            timeout=self.default_timeout,
        )
        return _point_verify_dto(response)
