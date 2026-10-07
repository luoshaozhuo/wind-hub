"""Commander gRPC Server。

本适配器实现由 commander.proto 生成的 CommanderService Servicer。
Wire contract 全部为强类型 Protobuf；业务行为仍委托 CommanderApp 应用服务。
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import grpc
from google.protobuf import empty_pb2, wrappers_pb2

from ...application.diagnostic import DeviceVerifyResult, PointVerifyResult
from ...application.errors import CommandError
from ...assembly import CommanderApp
from . import commander_pb2 as pb
from . import commander_pb2_grpc as pb_grpc
from .io_codec import (
    command_from_proto,
    command_result_to_proto,
    encode_scalar,
    point_value_to_proto,
)


async def _abort(context: grpc.aio.ServicerContext, exc: Exception) -> None:
    """把应用异常映射为稳定 gRPC status。"""
    if isinstance(exc, CommandError | KeyError):
        await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
    if isinstance(exc, ValueError):
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
    await context.abort(grpc.StatusCode.INTERNAL, str(exc) or type(exc).__name__)


def _required(value: str, field: str) -> str:
    """校验 protobuf 必填字符串。"""
    if not value:
        raise ValueError(f"missing required field '{field}'")
    return value


def _address_fields(data: dict[str, object] | None) -> list[pb.AddressField]:
    """把协议地址字典转换为稳定有序的 AddressField 列表。"""
    if not data:
        return []
    return [
        pb.AddressField(key=key, value=encode_scalar(value)) for key, value in sorted(data.items())
    ]


def _device_verify_to_proto(result: DeviceVerifyResult) -> pb.DeviceVerifyResponse:
    """把 Commander 设备诊断结果转换为 wire message。"""
    response = pb.DeviceVerifyResponse(
        device_id=result.device_id,
        protocol=result.protocol,
        host=result.host,
        port=result.port or 0,
        ok=result.ok,
    )
    response.stages.extend(
        pb.DiagnosticStageMessage(
            name=stage.name,
            ok=stage.ok,
            code=stage.code.value,
            severity=stage.severity.value,
            message=stage.message,
        )
        for stage in result.stages
    )
    return response


def _point_verify_to_proto(result: PointVerifyResult) -> pb.PointVerifyResponse:
    """把 Commander 点诊断结果转换为 wire message。"""
    response = pb.PointVerifyResponse(
        device_id=result.device_id,
        point_id=result.point_id,
        variable_name=result.variable_name or "",
        protocol=result.protocol,
        configured_address=_address_fields(result.configured_address),
        resolved_address=_address_fields(result.resolved_address),
        data_type=result.data_type,
        scale=result.scale,
        offset=result.offset,
        unit=result.unit,
        ok=result.ok,
        code=result.code.value,
        severity=result.severity.value,
        raw_value=encode_scalar(result.raw_value),
        engineering_value=encode_scalar(result.engineering_value),
        quality=result.quality or "",
        source=result.source or "",
        error=result.error or "",
    )
    if result.readable is not None:
        response.readable.CopyFrom(wrappers_pb2.BoolValue(value=result.readable))
    return response


class CommanderService(pb_grpc.CommanderServiceServicer):
    """Commander 强类型 gRPC 应用服务。"""

    def __init__(self, app: CommanderApp) -> None:
        self._app = app

    async def GetStatus(
        self, request: empty_pb2.Empty, context: grpc.aio.ServicerContext
    ) -> pb.CommanderStatusResponse:
        """返回 Commander 运行与配置状态。"""
        del request, context
        healthy = sum(1 for device in self._app.runtime.devices.values() if device.health().healthy)
        return pb.CommanderStatusResponse(
            running=True,
            device_count=len(self._app.runtime.devices),
            healthy_devices=healthy,
            active_revision=self._app.runtime.active_revision,
            active_config_hash=self._app.runtime.active_config_hash,
            prepared_revision=self._app.runtime.prepared_revision or "",
            prepared_config_hash=self._app.runtime.prepared_config_hash or "",
        )

    async def ListDevices(
        self, request: empty_pb2.Empty, context: grpc.aio.ServicerContext
    ) -> pb.ListDevicesResponse:
        """列出 Commander 当前设备会话。

        新架构的 CommanderRuntime 只为 enabled 设备建会话（disabled 设备在
        配置加载期排除），因此 ``enabled`` 恒为 True。
        """
        del request, context
        response = pb.ListDevicesResponse()
        response.devices.extend(
            pb.DeviceSummary(
                device_id=str(device.device_id),
                protocol=device.protocol_name,
                host=device.device.endpoint.host,
                port=device.device.endpoint.port or 0,
                enabled=True,
                healthy=device.health().healthy,
            )
            for device in self._app.runtime.devices.values()
        )
        return response

    async def PrepareConfig(
        self,
        request: pb.PrepareConfigRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.PrepareConfigResponse:
        """加载候选配置并构造 prepared generation。"""
        try:
            revision_id = _required(request.revision_id, "revision_id")
            expected_hash = _required(request.config_hash, "config_hash")
            actual_hash = await self._app.config.prepare_config(revision_id, expected_hash)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return pb.PrepareConfigResponse(
            success=True,
            revision_id=revision_id,
            config_hash=actual_hash,
        )

    async def ActivateConfig(
        self,
        request: pb.ActivateConfigRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.ActivateConfigResponse:
        """激活指定 prepared revision。"""
        try:
            revision_id = _required(request.revision_id, "revision_id")
            await self._app.config.activate_config(revision_id)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return pb.ActivateConfigResponse(
            success=True,
            revision_id=revision_id,
            active_config_hash=self._app.runtime.active_config_hash,
        )

    async def AbortConfig(
        self,
        request: pb.AbortConfigRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.AbortConfigResponse:
        """幂等撤销指定 prepared revision。"""
        try:
            revision_id = _required(request.revision_id, "revision_id")
            aborted = await self._app.config.abort_config(revision_id)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return pb.AbortConfigResponse(
            success=True,
            revision_id=revision_id,
            aborted=aborted,
        )

    async def ReadPoint(
        self,
        request: pb.ReadPointRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.PointValueMessage:
        """强类型单点读取。"""
        try:
            value = await self._app.read.read_point(
                _required(request.device_id, "device_id"),
                _required(request.point_id, "point_id"),
            )
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return point_value_to_proto(value)

    async def ReadPoints(
        self,
        request: pb.ReadPointsRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.ReadPointsResponse:
        """强类型批量读取。"""
        try:
            device_id = _required(request.device_id, "device_id")
            if not request.point_ids:
                raise ValueError("point_ids must not be empty")
            values = await self._app.read.read_points(device_id, list(request.point_ids))
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        response = pb.ReadPointsResponse()
        response.values.extend(point_value_to_proto(value) for value in values)
        return response

    async def WritePoint(
        self,
        request: pb.WritePointRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.CommandResultMessage:
        """强类型单点写入。"""
        try:
            _required(request.device_id, "device_id")
            _required(request.point_id, "point_id")
            command = command_from_proto(request)
            if not command.command_id:
                command = replace(command, command_id=uuid4().hex)
            result = await self._app.command.send(command)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return command_result_to_proto(result)

    async def WritePoints(
        self,
        request: pb.WritePointsRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.WritePointsResponse:
        """强类型批量写入。"""
        try:
            if not request.commands:
                raise ValueError("commands must not be empty")
            commands = [command_from_proto(item) for item in request.commands]
            commands = [
                replace(command, command_id=uuid4().hex) if not command.command_id else command
                for command in commands
            ]
            results = await self._app.command.send_batch(commands)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        response = pb.WritePointsResponse()
        response.results.extend(command_result_to_proto(result) for result in results)
        return response

    async def VerifyDevice(
        self,
        request: pb.VerifyDeviceRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.DeviceVerifyResponse:
        """执行设备分层链路诊断。"""
        try:
            result = await self._app.diagnostic.verify_device(
                _required(request.device_id, "device_id"),
                timeout=request.timeout or 1.0,
            )
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _device_verify_to_proto(result)

    async def ResolvePoint(
        self,
        request: pb.PointRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.PointVerifyResponse:
        """解析单点协议地址。"""
        return await self._point_diagnostic(request, context, resolve=True)

    async def VerifyPoint(
        self,
        request: pb.PointRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.PointVerifyResponse:
        """执行单点在线读取验证。"""
        return await self._point_diagnostic(request, context, resolve=False)

    async def VerifyPoints(
        self,
        request: pb.VerifyPointsRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb.PointsVerifyResponse:
        """批量验证设备点表或指定 point_group。"""
        try:
            result = await self._app.diagnostic.verify_points(
                _required(request.device_id, "device_id"),
                point_group=request.point_group or None,
            )
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        response = pb.PointsVerifyResponse(
            device_id=result.device_id,
            point_group=result.point_group or "",
            checked=result.checked,
            passed=result.passed,
            failed=result.failed,
            ok=result.ok,
        )
        response.points.extend(_point_verify_to_proto(item) for item in result.points)
        return response

    async def _point_diagnostic(
        self,
        request: pb.PointRequest,
        context: grpc.aio.ServicerContext,
        *,
        resolve: bool,
    ) -> pb.PointVerifyResponse:
        """执行单点 resolve/verify，并统一异常映射。"""
        try:
            device_id = _required(request.device_id, "device_id")
            point_id = _required(request.point_id, "point_id")
            if resolve:
                result = await self._app.diagnostic.resolve_point(device_id, point_id)
            else:
                result = await self._app.diagnostic.verify_point(device_id, point_id)
        except Exception as exc:
            await _abort(context, exc)
            raise AssertionError("context.abort must terminate the RPC") from exc
        return _point_verify_to_proto(result)


class CommanderGrpcServer:
    """Commander gRPC Server 生命周期包装。"""

    def __init__(self, server: grpc.aio.Server, endpoint: str) -> None:
        self._server = server
        self.endpoint = endpoint

    async def start(self) -> None:
        """开始监听 Commander gRPC endpoint。"""
        await self._server.start()

    async def stop(self, grace: float = 5.0) -> None:
        """停止接收新 RPC，并给在途请求有限宽限期。"""
        await self._server.stop(grace)


def build_grpc_server(
    app: CommanderApp,
    *,
    host: str,
    port: int,
) -> CommanderGrpcServer:
    """构建 Commander gRPC Server，但不开始监听。"""
    server = grpc.aio.server()
    pb_grpc.add_CommanderServiceServicer_to_server(CommanderService(app), server)
    requested = f"{host}:{port}"
    bound = server.add_insecure_port(requested)
    if bound == 0:
        raise RuntimeError(f"failed to bind Commander gRPC endpoint {requested}")
    return CommanderGrpcServer(server, f"{host}:{bound}")
