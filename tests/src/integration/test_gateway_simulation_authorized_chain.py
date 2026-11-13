"""S3 真实链路：Commander 授权的 Gateway 仿真执行 RPC → mTLS → Data Generator。

替代已删除的 ``kind=data_generator`` 直连夹具（直连转发在生产侧已移除）。
本测试不启动 Modbus 工业口，聚焦 R2-A 授权控制链：

- 真实 Gateway runtime/gRPC（mTLS、CN 权限、revision fencing、点映射校验）；
- 真实 GeneratorRpcClient → 脚本化 Data Generator grpc.aio Servicer（同 CA 双向认证）；
- ExecuteSimulationCommand 受理 ACCEPTED 与 GetSimulationExecutionResult
  的 PENDING→APPLIED 证据提升；
- 无权限 CN 拒止、同 request_id 幂等不重复执行、UNKNOWN 证据隔离。

PKI 在 tmp_path 独立生成，私钥仅存在于临时目录，不入库。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc
from contracts.scalar_codec import encode_scalar
from tests.support.gateway_v1 import (
    COMMAND_FLOAT,
    ServerSpec,
    reserve_port,
    start_gateway_v1,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

GATEWAY_ID = "gw1"
DEVICE_ID = "D01"
SCENARIO_ID = "wind-plant-physical"
GENERATION_ID = "gen-chain-001"
COMMANDER_CN = "gw-commander"
STRANGER_CN = "gw-stranger"


class ScriptedGeneratorService(dg_rpc.DataGeneratorServiceServicer):
    """脚本化 Data Generator：记录 UpdateVariables 送达并按队列返回执行证据。"""

    def __init__(self, *, evidence: list[str] | None = None, update_accepted: bool = True) -> None:
        self._evidence = list(evidence or [])
        self._update_accepted = update_accepted
        self.update_requests: list[dg.UpdateVariablesRequest] = []
        self.execution_requests: list[dg.GetInputExecutionRequest] = []

    async def GetDevice(  # noqa: N802 - protobuf servicer 方法名由 .proto 强制
        self, request: common.ObjectId, context: grpc.aio.ServicerContext
    ) -> dg.GetDeviceResponse:
        return dg.GetDeviceResponse(
            device=dg.Device(
                device_id=request.id,
                generation_id=GENERATION_ID,
                actual_state="RUNNING",
            )
        )

    async def GetGeneration(  # noqa: N802 - protobuf servicer 方法名由 .proto 强制
        self, request: common.ObjectId, context: grpc.aio.ServicerContext
    ) -> dg.GetGenerationResponse:
        return dg.GetGenerationResponse(
            generation=dg.Generation(
                generation_id=GENERATION_ID,
                scenario_id=SCENARIO_ID,
                actual_state="RUNNING",
                desired_state="RUNNING",
            )
        )

    async def ListVariables(  # noqa: N802 - protobuf servicer 方法名由 .proto 强制
        self, request: dg.ListVariablesRequest, context: grpc.aio.ServicerContext
    ) -> dg.ListVariablesResponse:
        return dg.ListVariablesResponse(
            generation_state="RUNNING",
            next_page_token="",
            variables=[
                dg.ModelVariable(point_id=COMMAND_FLOAT, writable=True, role="INPUT"),
            ],
        )

    async def UpdateVariables(  # noqa: N802 - protobuf servicer 方法名由 .proto 强制
        self, request: dg.UpdateVariablesRequest, context: grpc.aio.ServicerContext
    ) -> dg.UpdateVariablesResponse:
        self.update_requests.append(request)
        return dg.UpdateVariablesResponse(
            accepted=self._update_accepted,
            effective_step=21 if self._update_accepted else 0,
            error_code="" if self._update_accepted else "INPUT_REJECTED",
        )

    async def GetInputExecution(  # noqa: N802 - protobuf servicer 方法名由 .proto 强制
        self, request: dg.GetInputExecutionRequest, context: grpc.aio.ServicerContext
    ) -> dg.GetInputExecutionResponse:
        self.execution_requests.append(request)
        state = self._evidence.pop(0) if self._evidence else "APPLIED"
        return dg.GetInputExecutionResponse(
            request_id=request.request_id,
            state=state,
            effective_step=21,
        )


async def _start_generator(
    pki: dict[str, Any], service: ScriptedGeneratorService, port: int
) -> grpc.aio.Server:
    server = grpc.aio.server()
    dg_rpc.add_DataGeneratorServiceServicer_to_server(service, server)
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
    server.add_secure_port(f"127.0.0.1:{port}", credentials)
    await server.start()
    return server


def _simulation_block(endpoint: str) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "tls": {
            "enabled": True,
            "cert_file": f"certs/client-{COMMANDER_CN}.crt",
            "key_file": f"certs/client-{COMMANDER_CN}.key",
            "ca_file": "certs/ca.crt",
        },
        "scenario_id": SCENARIO_ID,
    }


async def _command_request(stub: Any, *, request_id: str) -> gw.SimulationCommandRequest:
    info = await stub.GetGateway(common.ObjectId(id=GATEWAY_ID))
    expires = Timestamp()
    expires.FromDatetime(datetime.now(UTC) + timedelta(seconds=5))
    return gw.SimulationCommandRequest(
        gateway_id=GATEWAY_ID,
        device_id=DEVICE_ID,
        point_id=COMMAND_FLOAT,
        value=encode_scalar(1500.0),
        request_id=request_id,
        expected_revision=info.gateway.revision,
        expires_at=expires,
    )


@pytest.fixture
async def chain(tmp_path: Path):
    generator = ScriptedGeneratorService(evidence=["PENDING", "APPLIED"])
    # 先占端口，仿真端点在 Gateway 配置中固定后再启动 Generator 服务。
    generator_port = reserve_port()
    spec = ServerSpec(
        server_id=GATEWAY_ID,
        device_id=DEVICE_ID,
        modbus_port=reserve_port(),
        slave_id=1,
    )
    harness = await start_gateway_v1(
        tmp_path / "root",
        [spec],
        mtls={
            COMMANDER_CN: ["simulation_execute", "read"],
            "gw-reader": ["read"],
        },
        bootstrap_cn=COMMANDER_CN,
        extra_clients=[STRANGER_CN],
        simulation=_simulation_block(f"127.0.0.1:{generator_port}"),
    )
    server = await _start_generator(harness.pki, generator, generator_port)
    try:
        yield harness, generator
    finally:
        await server.stop(1.0)
        await harness.stop()


async def test_execute_accepted_then_result_pending_to_applied(chain: Any) -> None:
    harness, generator = chain
    channel = harness.mtls_channel(COMMANDER_CN)
    try:
        stub = gw_rpc.IndustrialGatewayServiceStub(channel)
        response = await stub.ExecuteSimulationCommand(
            await _command_request(stub, request_id="req-chain-001")
        )
        assert response.accepted is True
        assert response.state == "ACCEPTED"
        assert response.effective_step == 21
        assert len(generator.update_requests) == 1
        assert generator.update_requests[0].request_id == "req-chain-001"

        first = await stub.GetSimulationExecutionResult(
            gw.SimulationExecutionQuery(gateway_id=GATEWAY_ID, request_id="req-chain-001")
        )
        assert first.accepted is True
        assert first.state == "PENDING"
        second = await stub.GetSimulationExecutionResult(
            gw.SimulationExecutionQuery(gateway_id=GATEWAY_ID, request_id="req-chain-001")
        )
        assert second.accepted is True
        assert second.state == "APPLIED"
        assert second.effective_step == 21
        assert [call.request_id for call in generator.execution_requests] == [
            "req-chain-001",
            "req-chain-001",
        ]
    finally:
        await channel.close()


async def test_stranger_cn_is_denied_simulation_permission(chain: Any) -> None:
    harness, generator = chain
    channel = harness.mtls_channel(STRANGER_CN)
    try:
        stub = gw_rpc.IndustrialGatewayServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as execute_error:
            await stub.ExecuteSimulationCommand(
                await _command_request(stub, request_id="req-denied-001")
            )
        assert execute_error.value.code() is grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.aio.AioRpcError) as query_error:
            await stub.GetSimulationExecutionResult(
                gw.SimulationExecutionQuery(gateway_id=GATEWAY_ID, request_id="req-denied-001")
            )
        assert query_error.value.code() is grpc.StatusCode.PERMISSION_DENIED
        assert generator.update_requests == []
    finally:
        await channel.close()


async def test_replayed_request_id_does_not_reexecute(chain: Any) -> None:
    harness, generator = chain
    channel = harness.mtls_channel(COMMANDER_CN)
    try:
        stub = gw_rpc.IndustrialGatewayServiceStub(channel)
        request = await _command_request(stub, request_id="req-replay-001")
        first = await stub.ExecuteSimulationCommand(request)
        second = await stub.ExecuteSimulationCommand(request)
        assert first.state == "ACCEPTED"
        assert second.state == "ACCEPTED"
        assert second.execution_id == first.execution_id
        assert len(generator.update_requests) == 1
    finally:
        await channel.close()


async def test_generator_unknown_evidence_stays_unknown(chain: Any) -> None:
    harness, _generator = chain
    channel = harness.mtls_channel(COMMANDER_CN)
    try:
        stub = gw_rpc.IndustrialGatewayServiceStub(channel)
        request_id = "req-unknown-001"
        accepted = await stub.ExecuteSimulationCommand(
            await _command_request(stub, request_id=request_id)
        )
        assert accepted.accepted is True
        # 夹具证据队列：PENDING 后 APPLIED；先消费 PENDING，再直接查询未知身份。
        await stub.GetSimulationExecutionResult(
            gw.SimulationExecutionQuery(gateway_id=GATEWAY_ID, request_id=request_id)
        )
        missing = await stub.GetSimulationExecutionResult(
            gw.SimulationExecutionQuery(gateway_id=GATEWAY_ID, request_id="req-never-seen")
        )
        assert missing.accepted is False
        assert missing.state == "UNKNOWN"
        assert missing.error_code == "EXECUTION_NOT_OBSERVED"
    finally:
        await channel.close()
