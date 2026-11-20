"""Web Server Commander v1 客户端：正式 RPC、Bearer 认证、单次提交。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic
from typing import Literal

import grpc

from contracts import commander_pb2 as cmd
from contracts import commander_pb2_grpc as cmd_rpc
from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from core.application.protocol_contract import WriteOutcome
from core.infrastructure.grpc_bearer import bearer_metadata
from web_server.adapter.outbound.grpc.common import GrpcClientBase
from web_server.application.device.command_model import Command, CommandResult
from web_server.application.port.worker import (
    CommanderStatus,
    ConfigApplyResult,
    ConfigPushResult,
    DeviceVerifyResult,
    DiagnosticStage,
    RemoteConfigSnapshot,
)

_TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED", "EXPIRED", "CANCELLED"}
_NOT_SENT = {"INTERRUPTED", "EXPIRED", "CANCELLED"}


def _encode_scalar(value: bool | int | float | str) -> common.ScalarValue:
    scalar = common.ScalarValue()
    if isinstance(value, bool):
        scalar.bool_value = value
    elif isinstance(value, int):
        if -(2**63) <= value < 0:
            scalar.sint64_value = value
        elif 0 <= value < 2**64:
            scalar.uint64_value = value
        else:
            raise ValueError("integer outside protobuf 64-bit scalar range")
    elif isinstance(value, float):
        scalar.double_value = value
    elif isinstance(value, str):
        scalar.string_value = value
    else:
        raise TypeError(f"unsupported command value: {type(value).__name__}")
    return scalar


class CommanderGrpcClient(GrpcClientBase):
    """Commander v1 只实现真实能力；无法确认的指令结果保持 UNKNOWN。"""

    def __init__(self, target: str, *, default_timeout: float = 5.0) -> None:
        super().__init__(target, default_timeout=default_timeout)
        self._commander = cmd_rpc.CommanderServiceStub(self._channel)
        self._common = common_rpc.CommonServiceStub(self._channel)

    @staticmethod
    def _metadata() -> tuple[tuple[str, str], ...]:
        return bearer_metadata("env:WIND_HUB_COMMANDER_OPERATOR_TOKEN")

    async def get_config_snapshot(self, scope: Literal["disk", "runtime"]) -> RemoteConfigSnapshot:
        wire_scope = (
            common.SNAPSHOT_SCOPE_DISK if scope == "disk" else common.SNAPSHOT_SCOPE_RUNTIME
        )
        response = await self._common.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=wire_scope),
            timeout=self.default_timeout,
            metadata=self._metadata(),
        )
        return RemoteConfigSnapshot(
            revision=response.revision,
            files={item.relative_path: bytes(item.content) for item in response.files},
        )

    async def push_config(
        self, files: dict[str, bytes], expected_revision: str
    ) -> ConfigPushResult:
        response = await self._common.PushConfigSnapshot(
            common.PushConfigSnapshotRequest(
                expected_revision=expected_revision,
                files=[
                    common.SnapshotFile(
                        relative_path=path,
                        content=content,
                        size_bytes=len(content),
                        sha256=sha256(content).hexdigest(),
                    )
                    for path, content in sorted(files.items())
                ],
            ),
            timeout=self.default_timeout,
            metadata=self._metadata(),
        )
        return ConfigPushResult(
            candidate_id=response.candidate_id,
            valid=response.valid,
            base_revision=response.base_revision,
            errors=[f"{item.path}: {item.message}" for item in response.validation],
        )

    async def apply_config(self, candidate_id: str, expected_revision: str) -> ConfigApplyResult:
        response = await self._common.ApplyConfigSnapshot(
            common.ApplyConfigSnapshotRequest(
                candidate_id=candidate_id,
                expected_revision=expected_revision,
            ),
            timeout=max(self.default_timeout, 30.0),
            metadata=self._metadata(),
        )
        errors = [
            f"{item.object_id}: {item.error_code} {item.message}"
            for item in response.apply_results
            if item.status not in {"APPLIED", "RESTART_REQUIRED"}
        ]
        return ConfigApplyResult(
            file_commit=response.file_commit,
            effective_revision=response.effective_revision,
            restart_required=response.restart_required,
            errors=errors,
        )

    async def _devices(self) -> list[cmd.Device]:
        devices: list[cmd.Device] = []
        token = ""
        while True:
            response = await self._commander.ListDevices(
                cmd.ListDevicesRequest(page_size=200, page_token=token),
                timeout=self.default_timeout,
                metadata=self._metadata(),
            )
            devices.extend(response.devices)
            token = response.next_page_token
            if not token:
                return devices

    async def status(self) -> CommanderStatus:
        health, runtime, devices = await asyncio.gather(
            self._common.GetServiceStatus(
                common.Empty(),
                timeout=self.default_timeout,
                metadata=self._metadata(),
            ),
            self.get_config_snapshot("runtime"),
            self._devices(),
        )
        return CommanderStatus(
            running=health.health in {"HEALTHY", "DEGRADED"},
            device_count=len(devices),
            active_revision=runtime.revision,
        )

    async def verify_device(self, device_id: str, timeout: float = 1.0) -> DeviceVerifyResult:
        if timeout <= 0:
            raise ValueError("diagnostic timeout must be positive")
        device, result = await asyncio.gather(
            self._commander.GetDevice(
                common.ObjectId(id=device_id),
                timeout=self.default_timeout,
                metadata=self._metadata(),
            ),
            self._common.Diagnose(
                common.DiagnoseRequest(
                    target_id=device_id,
                    stages=["device", "tcp", "protocol"],
                    timeout_ms=max(1, int(timeout * 1000)),
                ),
                timeout=max(self.default_timeout, timeout + 1),
                metadata=self._metadata(),
            ),
        )
        endpoint = device.device.endpoint
        host = endpoint
        port = 0
        if ":" in endpoint:
            host, _, port_text = endpoint.rpartition(":")
            if port_text.isdecimal():
                port = int(port_text)
        stages = [
            DiagnosticStage(
                name=stage.stage,
                ok=stage.status == "PASS",
                code=stage.error_code or stage.status,
                severity="error" if stage.status in {"FAIL", "TIMEOUT"} else "info",
                message=stage.message,
            )
            for stage in result.stages
        ]
        return DeviceVerifyResult(
            device_id=device_id,
            protocol=device.device.protocol,
            host=host,
            port=port,
            ok=result.overall_status == "PASS",
            stages=stages,
        )

    async def write(self, command: Command) -> CommandResult:
        """提交最多一次；RPC 状态不确定时不重新提交现场控制指令。"""
        if command.timeout <= 0:
            raise ValueError("write timeout must be positive")
        deadline = monotonic() + command.timeout

        def outcome(value: WriteOutcome, error: str | None = None) -> CommandResult:
            return CommandResult(
                command_id=command.command_id,
                outcome=value,
                error=error,
                finished_at=datetime.now(UTC),
            )

        # 获取修订号失败发生在提交之前，可以确定没有执行本次控制。
        try:
            device = await self._commander.GetDevice(
                common.ObjectId(id=command.device_id),
                timeout=min(self.default_timeout, command.timeout),
                metadata=self._metadata(),
            )
            revision = device.device.revision
            if not revision:
                return outcome(WriteOutcome.CONFIRMED_FAILURE, "device has no active revision")
        except grpc.RpcError as exc:
            return outcome(WriteOutcome.CONFIRMED_FAILURE, f"revision query failed: {exc}")

        remaining = deadline - monotonic()
        if remaining <= 0:
            return outcome(WriteOutcome.CONFIRMED_FAILURE, "expired before command submission")
        expires = datetime.now(UTC) + timedelta(seconds=remaining)
        request = cmd.SubmitCommandRequest(
            device_id=command.device_id,
            point_id=command.point_id,
            value=_encode_scalar(command.value),
            expected_revision=revision,
            request_id=command.command_id,
        )
        request.expires_at.FromDatetime(expires)
        try:
            admitted = await self._commander.SubmitCommand(
                request,
                timeout=remaining,
                metadata=self._metadata(),
            )
        except (grpc.RpcError, TimeoutError) as exc:
            return outcome(
                WriteOutcome.UNKNOWN,
                f"submission outcome unknown; command was not retried: {exc}",
            )
        if not admitted.accepted:
            return outcome(
                WriteOutcome.CONFIRMED_FAILURE,
                admitted.error_code or admitted.message or "command rejected",
            )
        if not admitted.command_id:
            return outcome(WriteOutcome.UNKNOWN, "accepted without queryable command ID")

        while monotonic() < deadline:
            remaining = deadline - monotonic()
            try:
                result = await self._commander.GetCommandResult(
                    cmd.GetCommandResultRequest(command_id=admitted.command_id),
                    timeout=remaining,
                    metadata=self._metadata(),
                )
            except (grpc.RpcError, TimeoutError) as exc:
                return outcome(
                    WriteOutcome.UNKNOWN,
                    f"accepted command {admitted.command_id} result unavailable: {exc}",
                )
            if result.state in _TERMINAL:
                if result.state == "SUCCEEDED" and result.outcome == "CONFIRMED_SUCCESS":
                    return outcome(WriteOutcome.CONFIRMED_SUCCESS)
                if result.state in _NOT_SENT and result.outcome == "NOT_SENT":
                    return outcome(
                        WriteOutcome.CONFIRMED_FAILURE,
                        result.error_code or result.message or result.state,
                    )
                if result.state == "FAILED" and result.outcome == "CONFIRMED_FAILURE":
                    return outcome(
                        WriteOutcome.CONFIRMED_FAILURE,
                        result.error_code or result.message or "confirmed failure",
                    )
                return outcome(
                    WriteOutcome.UNKNOWN,
                    result.error_code or result.message or "unconfirmed native I/O",
                )
            await asyncio.sleep(min(0.05, max(0.0, deadline - monotonic())))
        return outcome(
            WriteOutcome.UNKNOWN,
            f"command {admitted.command_id} is not terminal; submission must not be retried",
        )
