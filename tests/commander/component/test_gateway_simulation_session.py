"""GatewaySimulationSession 组件测试：真实 mTLS 假 Gateway 上的 APPLIED 确认闭环。

覆盖：
- 受理后轮询 PENDING/APPLIED，仅 APPLIED 结算 CONFIRMED_SUCCESS；
- REJECTED 立即 CONFIRMED_FAILURE，UNKNOWN 立即 UNKNOWN；
- 确认超时结算 UNKNOWN 且不产生新的执行请求（禁止新身份盲重试）；
- 同 command_id 重放保持单一 request_id；
- 无命令身份的 write_one/write_many 被显式拒绝。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import grpc
import pytest

from commander.application.settings import SimulationGatewayTarget
from commander.domain.command import AcceptedCommand, CommandSpec
from commander.infrastructure.adapters.outbound.gateway import GatewaySimulationSession
from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc
from core.application.protocol_contract import WriteOutcome
from tests.support.gateway_v1 import reserve_port, write_mtls_pki

pytestmark = [pytest.mark.component, pytest.mark.real_service]

GATEWAY_ID = "gw-wtg-sim01"
DEVICE_ID = "WTG_SIM01"
POINT_ID = "power_limit_setpoint"


class ScriptedGatewayService(gw_rpc.IndustrialGatewayServiceServicer):
    """仅实现仿真闭环三 RPC 的脚本化 Gateway，其余方法保持 UNIMPLEMENTED。"""

    def __init__(
        self,
        *,
        execute_state: str = "ACCEPTED",
        execute_accepted: bool = True,
        evidence: list[gw.SimulationCommandResponse] | None = None,
        revision: str = "rev-42",
    ) -> None:
        self._execute_state = execute_state
        self._execute_accepted = execute_accepted
        self._evidence = list(evidence or [])
        self._revision = revision
        self.execute_calls: list[gw.SimulationCommandRequest] = []
        self.query_calls: list[gw.SimulationExecutionQuery] = []

    async def GetGateway(  # noqa: N802 - protobuf servicer 方法名由 .proto 强制
        self, request: common.ObjectId, context: grpc.aio.ServicerContext
    ) -> gw.GetGatewayResponse:
        return gw.GetGatewayResponse(
            gateway=gw.Gateway(gateway_id=request.id, revision=self._revision)
        )

    async def ExecuteSimulationCommand(  # noqa: N802 - protobuf servicer 方法名由 .proto 强制
        self, request: gw.SimulationCommandRequest, context: grpc.aio.ServicerContext
    ) -> gw.SimulationCommandResponse:
        self.execute_calls.append(request)
        return gw.SimulationCommandResponse(
            request_id=request.request_id,
            accepted=self._execute_accepted,
            execution_id="exec-1",
            state=self._execute_state,
            effective_step=9,
        )

    async def GetSimulationExecutionResult(  # noqa: N802 - protobuf servicer 方法名由 .proto 强制
        self, request: gw.SimulationExecutionQuery, context: grpc.aio.ServicerContext
    ) -> gw.SimulationCommandResponse:
        self.query_calls.append(request)
        if self._evidence:
            response = self._evidence.pop(0)
            return response
        return gw.SimulationCommandResponse(
            request_id=request.request_id,
            accepted=True,
            execution_id="exec-1",
            state="APPLIED",
            effective_step=9,
        )


async def _start(
    tmp_path: Path, service: ScriptedGatewayService
) -> tuple[GatewaySimulationSession, ScriptedGatewayService, grpc.aio.Server]:
    pki = write_mtls_pki(tmp_path, ["commander-sim"])
    server = grpc.aio.server()
    gw_rpc.add_IndustrialGatewayServiceServicer_to_server(service, server)
    credentials = grpc.ssl_server_credentials(
        [
            (
                Path(pki["server_key"]).read_bytes(),
                Path(pki["server_crt"]).read_bytes(),
            )
        ],
        root_certificates=Path(pki["ca"]).read_bytes(),
        require_client_auth=True,
    )
    port = reserve_port()
    server.add_secure_port(f"127.0.0.1:{port}", credentials)
    await server.start()
    cert, key = pki["clients"]["commander-sim"]
    target = SimulationGatewayTarget(
        gateway_id=GATEWAY_ID,
        endpoint=f"127.0.0.1:{port}",
        ca_cert=Path(pki["ca"]),
        client_cert=cert,
        client_key=key,
    )
    session = GatewaySimulationSession(DEVICE_ID, target)
    await session.connect()
    return session, service, server


def _command(command_id: str, *, ttl_seconds: float = 5.0) -> AcceptedCommand:
    return AcceptedCommand(
        command_id=command_id,
        request_id=command_id,
        caller_id="operator-cn",
        spec=CommandSpec(
            device_id=DEVICE_ID,
            point_id=POINT_ID,
            value=1500.0,
            config_reference="WTG_SIM01/power_limit_setpoint",
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        ),
        received_at=datetime.now(UTC),
        receive_seq=1,
    )


async def test_applied_only_after_pending_polling(tmp_path: Path) -> None:
    service = ScriptedGatewayService(
        evidence=[
            gw.SimulationCommandResponse(
                request_id="cmd-001", accepted=True, state="PENDING", effective_step=9
            ),
            gw.SimulationCommandResponse(
                request_id="cmd-001", accepted=True, state="APPLIED", effective_step=9
            ),
        ]
    )
    session, service, server = await _start(tmp_path, service)
    try:
        result = await session.write_authorized(_command("cmd-001"))
        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert len(service.execute_calls) == 1
        assert service.execute_calls[0].request_id == "cmd-001"
        assert service.execute_calls[0].expected_revision == "rev-42"
        assert [call.request_id for call in service.query_calls] == ["cmd-001", "cmd-001"]
    finally:
        await session.close()
        await server.stop(1.0)


async def test_immediate_applied_needs_no_polling(tmp_path: Path) -> None:
    service = ScriptedGatewayService(execute_state="APPLIED")
    session, service, server = await _start(tmp_path, service)
    try:
        result = await session.write_authorized(_command("cmd-002"))
        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert service.query_calls == []
    finally:
        await session.close()
        await server.stop(1.0)


async def test_rejected_is_confirmed_failure(tmp_path: Path) -> None:
    service = ScriptedGatewayService(execute_state="REJECTED", execute_accepted=False)
    session, service, server = await _start(tmp_path, service)
    try:
        result = await session.write_authorized(_command("cmd-003"))
        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert service.query_calls == []
    finally:
        await session.close()
        await server.stop(1.0)


async def test_unknown_execution_never_settles_success(tmp_path: Path) -> None:
    service = ScriptedGatewayService(
        evidence=[
            gw.SimulationCommandResponse(
                request_id="cmd-004",
                accepted=True,
                state="UNKNOWN",
                error_code="EXECUTION_QUERY_FAILED",
            )
        ]
    )
    session, service, server = await _start(tmp_path, service)
    try:
        result = await session.write_authorized(_command("cmd-004"))
        assert result.outcome is WriteOutcome.UNKNOWN
        assert result.message == "EXECUTION_QUERY_FAILED"
        assert len(service.execute_calls) == 1
    finally:
        await session.close()
        await server.stop(1.0)


async def test_confirmation_timeout_is_unknown_without_resubmission(tmp_path: Path) -> None:
    # 网关始终回答 PENDING；命令 0.4s 过期，必须以 UNKNOWN 结束且只执行一次。
    service = ScriptedGatewayService(
        evidence=[
            gw.SimulationCommandResponse(request_id="cmd-timeout", accepted=True, state="PENDING")
        ]
        * 32
    )
    session, service, server = await _start(tmp_path, service)
    try:
        result = await session.write_authorized(_command("cmd-timeout", ttl_seconds=0.4))
        assert result.outcome is WriteOutcome.UNKNOWN
        # 轮询窗口在 sleep 后耗尽时走 break 分支：终态 UNKNOWN 确定，
        # 消息保留最后观测状态 PENDING（专属超时码仅在 sleep 前判定时产生）。
        assert result.message in {"EXECUTION_CONFIRMATION_TIMEOUT", "PENDING"}
        assert len(service.execute_calls) == 1
        assert {call.request_id for call in service.execute_calls} == {"cmd-timeout"}
    finally:
        await session.close()
        await server.stop(1.0)


async def test_replay_keeps_single_command_identity(tmp_path: Path) -> None:
    service = ScriptedGatewayService()
    session, service, server = await _start(tmp_path, service)
    try:
        command = _command("cmd-replay")
        first = await session.write_authorized(command)
        second = await session.write_authorized(command)
        assert first.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert second.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert [call.request_id for call in service.execute_calls] == [
            "cmd-replay",
            "cmd-replay",
        ]
    finally:
        await session.close()
        await server.stop(1.0)


async def test_raw_writes_are_refused(tmp_path: Path) -> None:
    service = ScriptedGatewayService()
    session, _service, server = await _start(tmp_path, service)
    try:
        with pytest.raises(RuntimeError, match="durable command identity"):
            await session.write_one(None)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="per-command identities"):
            await session.write_many([])
    finally:
        await session.close()
        await server.stop(1.0)
