"""GeneratorRpcClient 组件测试：真实 mTLS 通道 + 脚本化 Data Generator Servicer。

覆盖 submit 的 Generation 绑定裁决链、get_execution 的证据白名单
（仅 PENDING/APPLIED 直通，其余与 request_id 不匹配一律 UNKNOWN），
以及明文降级拒绝。不使用任何 Mock 通道。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from industrial_gateway.infrastructure.adapters.outbound.grpc.generator_client import (
    GeneratorRpcClient,
)
from industrial_gateway.infrastructure.config.schema import TlsConfig
from tests.support.gateway_v1 import reserve_port, write_mtls_pki

pytestmark = [pytest.mark.component, pytest.mark.real_service]

DEVICE_ID = "WTG_SIM01"
SCENARIO_ID = "wind-plant-physical"
GENERATION_ID = "gen-001"
POINT_ID = "power_limit_setpoint"


class ScriptedGeneratorService(dg_rpc.DataGeneratorServiceServicer):
    """按脚本应答的真实 grpc.aio 服务，记录调用与 request_id。"""

    def __init__(
        self,
        *,
        evidence_state: str = "APPLIED",
        evidence_request_id: str | None = None,
        update_accepted: bool = True,
    ) -> None:
        self.evidence_state = evidence_state
        self.evidence_request_id = evidence_request_id
        self.update_accepted = update_accepted
        self.update_calls: list[dg.UpdateVariablesRequest] = []
        self.execution_calls: list[dg.GetInputExecutionRequest] = []

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
                dg.ModelVariable(
                    point_id=POINT_ID,
                    writable=True,
                    role="INPUT",
                )
            ],
        )

    async def UpdateVariables(  # noqa: N802 - protobuf servicer 方法名由 .proto 强制
        self, request: dg.UpdateVariablesRequest, context: grpc.aio.ServicerContext
    ) -> dg.UpdateVariablesResponse:
        self.update_calls.append(request)
        return dg.UpdateVariablesResponse(
            accepted=self.update_accepted,
            effective_step=12 if self.update_accepted else 0,
            error_code="" if self.update_accepted else "INPUT_REJECTED",
        )

    async def GetInputExecution(  # noqa: N802 - protobuf servicer 方法名由 .proto 强制
        self, request: dg.GetInputExecutionRequest, context: grpc.aio.ServicerContext
    ) -> dg.GetInputExecutionResponse:
        self.execution_calls.append(request)
        return dg.GetInputExecutionResponse(
            request_id=self.evidence_request_id
            if self.evidence_request_id is not None
            else request.request_id,
            state=self.evidence_state,
            effective_step=12,
        )


async def _start_server(
    tmp_path: Path, service: ScriptedGeneratorService
) -> tuple[str, Any, grpc.aio.Server]:
    pki = write_mtls_pki(tmp_path, ["gw-sim-client"])
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
    port = reserve_port()
    server.add_secure_port(f"127.0.0.1:{port}", credentials)
    await server.start()
    return f"127.0.0.1:{port}", pki, server


def _client(endpoint: str, pki: dict[str, Any], root: Path) -> GeneratorRpcClient:
    cert, key = pki["clients"]["gw-sim-client"]
    return GeneratorRpcClient(
        endpoint=endpoint,
        tls=TlsConfig(
            enabled=True,
            cert_file=Path("certs") / cert.name,
            key_file=Path("certs") / key.name,
            ca_file=Path("certs") / Path(pki["ca"]).name,
        ),
        project_root=root,
        scenario_id=SCENARIO_ID,
    )


async def test_submit_resolves_generation_and_preserves_request_identity(tmp_path: Path) -> None:
    service = ScriptedGeneratorService()
    endpoint, pki, server = await _start_server(tmp_path, service)
    client = _client(endpoint, pki, tmp_path)
    client.start()
    try:
        receipt = await client.submit(
            device_id=DEVICE_ID,
            point_id=POINT_ID,
            value=1500.0,
            request_id="req-submit-1",
            timeout=3.0,
        )
        assert receipt.accepted is True
        assert receipt.effective_step == 12
        assert receipt.generation_id == GENERATION_ID
        assert len(service.update_calls) == 1
        assert service.update_calls[0].request_id == "req-submit-1"
        assert service.update_calls[0].generation_id == GENERATION_ID
    finally:
        await client.close()
        await server.stop(1.0)


async def test_get_execution_whitelists_states_and_validates_echoed_request_id(
    tmp_path: Path,
) -> None:
    applied = ScriptedGeneratorService(evidence_state="APPLIED")
    endpoint, pki, server = await _start_server(tmp_path, applied)
    client = _client(endpoint, pki, tmp_path)
    client.start()
    try:
        assert (
            await client.get_execution(
                generation_id=GENERATION_ID, request_id="req-ev-1", timeout=3.0
            )
            == "APPLIED"
        )
        assert applied.execution_calls[0].generation_id == GENERATION_ID
    finally:
        await client.close()
        await server.stop(1.0)

    pending = ScriptedGeneratorService(evidence_state="PENDING")
    endpoint, pki, server = await _start_server(tmp_path, pending)
    client = _client(endpoint, pki, tmp_path)
    client.start()
    try:
        assert (
            await client.get_execution(
                generation_id=GENERATION_ID, request_id="req-ev-2", timeout=3.0
            )
            == "PENDING"
        )
    finally:
        await client.close()
        await server.stop(1.0)

    rejected = ScriptedGeneratorService(evidence_state="REJECTED")
    endpoint, pki, server = await _start_server(tmp_path, rejected)
    client = _client(endpoint, pki, tmp_path)
    client.start()
    try:
        assert (
            await client.get_execution(
                generation_id=GENERATION_ID, request_id="req-ev-3", timeout=3.0
            )
            == "UNKNOWN"
        )
    finally:
        await client.close()
        await server.stop(1.0)

    mismatched = ScriptedGeneratorService(evidence_state="APPLIED", evidence_request_id="req-other")
    endpoint, pki, server = await _start_server(tmp_path, mismatched)
    client = _client(endpoint, pki, tmp_path)
    client.start()
    try:
        # 服务端回显错误 request_id 时，即使状态为 APPLIED 也不得采信。
        assert (
            await client.get_execution(
                generation_id=GENERATION_ID, request_id="req-ev-4", timeout=3.0
            )
            == "UNKNOWN"
        )
    finally:
        await client.close()
        await server.stop(1.0)


async def test_plaintext_or_incomplete_certificates_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutual TLS"):
        GeneratorRpcClient(
            endpoint="127.0.0.1:1",
            tls=TlsConfig(enabled=False),
            project_root=tmp_path,
            scenario_id=SCENARIO_ID,
        ).start()
    with pytest.raises(ValueError, match="certificate"):
        GeneratorRpcClient(
            endpoint="127.0.0.1:1",
            tls=TlsConfig(enabled=True),
            project_root=tmp_path,
            scenario_id=SCENARIO_ID,
        )


async def test_rejected_update_returns_not_accepted_receipt(tmp_path: Path) -> None:
    service = ScriptedGeneratorService(update_accepted=False)
    endpoint, pki, server = await _start_server(tmp_path, service)
    client = _client(endpoint, pki, tmp_path)
    client.start()
    try:
        receipt = await client.submit(
            device_id=DEVICE_ID,
            point_id=POINT_ID,
            value=1500.0,
            request_id="req-reject-1",
            timeout=3.0,
        )
        assert receipt.accepted is False
        assert receipt.error_code == "INPUT_REJECTED"
    finally:
        await client.close()
        await server.stop(1.0)
