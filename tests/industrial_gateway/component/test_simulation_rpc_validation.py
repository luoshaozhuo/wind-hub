"""两个仿真执行 RPC 的 gRPC 处理器校验矩阵（真实 Gateway runtime + mTLS）。

授权成功路径在 tests/src/integration/test_gateway_simulation_authorized_chain.py
覆盖；本文件聚焦拒绝路径：revision fencing、点映射、过期、未配置、明文拒止、
空 request_id 与未知 gateway 实例。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc
from contracts.scalar_codec import encode_scalar
from tests.support.gateway_v1 import (
    COMMAND_FLOAT,
    ServerSpec,
    reserve_port,
    start_gateway_v1,
)

pytestmark = [pytest.mark.component, pytest.mark.real_service]

GATEWAY_ID = "gw1"
DEVICE_ID = "D01"
COMMANDER_CN = "gw-commander"
STRANGER_CN = "gw-stranger"


def _simulation_block(endpoint: str) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "tls": {
            "enabled": True,
            "cert_file": f"certs/client-{COMMANDER_CN}.crt",
            "key_file": f"certs/client-{COMMANDER_CN}.key",
            "ca_file": "certs/ca.crt",
        },
        "scenario_id": "wind-plant-physical",
    }


def _expires(seconds: float = 5.0) -> Timestamp:
    timestamp = Timestamp()
    timestamp.FromDatetime(datetime.now(UTC) + timedelta(seconds=seconds))
    return timestamp


@pytest.fixture
async def harness(tmp_path: Path):
    spec = ServerSpec(
        server_id=GATEWAY_ID,
        device_id=DEVICE_ID,
        modbus_port=reserve_port(),
        slave_id=1,
    )
    gateway = await start_gateway_v1(
        tmp_path / "root",
        [spec],
        mtls={COMMANDER_CN: ["simulation_execute", "read"]},
        bootstrap_cn=COMMANDER_CN,
        extra_clients=[STRANGER_CN],
        simulation=_simulation_block(f"127.0.0.1:{reserve_port()}"),
    )
    try:
        yield gateway
    finally:
        await gateway.stop()


async def _stub(harness: Any, cn: str) -> tuple[gw_rpc.IndustrialGatewayServiceStub, Any]:
    channel = harness.mtls_channel(cn)
    return gw_rpc.IndustrialGatewayServiceStub(channel), channel


async def test_revision_conflict_aborts(harness: Any) -> None:
    stub, channel = await _stub(harness, COMMANDER_CN)
    try:
        info = await stub.GetGateway(common.ObjectId(id=GATEWAY_ID))
        request = gw.SimulationCommandRequest(
            gateway_id=GATEWAY_ID,
            device_id=DEVICE_ID,
            point_id=COMMAND_FLOAT,
            value=encode_scalar(1500.0),
            request_id="req-rev-1",
            expected_revision=info.gateway.revision + "-stale",
            expires_at=_expires(),
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.ExecuteSimulationCommand(request)
        assert error.value.code() is grpc.StatusCode.ABORTED
    finally:
        await channel.close()


async def test_unmapped_point_is_permission_denied(harness: Any) -> None:
    stub, channel = await _stub(harness, COMMANDER_CN)
    try:
        info = await stub.GetGateway(common.ObjectId(id=GATEWAY_ID))
        request = gw.SimulationCommandRequest(
            gateway_id=GATEWAY_ID,
            device_id=DEVICE_ID,
            point_id="not_an_authorized_point",
            value=encode_scalar(1.0),
            request_id="req-point-1",
            expected_revision=info.gateway.revision,
            expires_at=_expires(),
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.ExecuteSimulationCommand(request)
        assert error.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


async def test_expired_command_is_deadline_exceeded(harness: Any) -> None:
    stub, channel = await _stub(harness, COMMANDER_CN)
    try:
        info = await stub.GetGateway(common.ObjectId(id=GATEWAY_ID))
        request = gw.SimulationCommandRequest(
            gateway_id=GATEWAY_ID,
            device_id=DEVICE_ID,
            point_id=COMMAND_FLOAT,
            value=encode_scalar(1500.0),
            request_id="req-expired-1",
            expected_revision=info.gateway.revision,
            expires_at=_expires(-1.0),
        )
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.ExecuteSimulationCommand(request)
        assert error.value.code() is grpc.StatusCode.DEADLINE_EXCEEDED
    finally:
        await channel.close()


async def test_unknown_gateway_is_not_found(harness: Any) -> None:
    stub, channel = await _stub(harness, COMMANDER_CN)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.GetSimulationExecutionResult(
                gw.SimulationExecutionQuery(gateway_id="gw-missing", request_id="req-x")
            )
        assert error.value.code() is grpc.StatusCode.NOT_FOUND
    finally:
        await channel.close()


async def test_blank_request_id_is_invalid_argument(harness: Any) -> None:
    stub, channel = await _stub(harness, COMMANDER_CN)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.GetSimulationExecutionResult(
                gw.SimulationExecutionQuery(gateway_id=GATEWAY_ID, request_id="  ")
            )
        assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await channel.close()


async def test_simulation_without_permission_is_denied(harness: Any) -> None:
    stub, channel = await _stub(harness, STRANGER_CN)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.GetSimulationExecutionResult(
                gw.SimulationExecutionQuery(gateway_id=GATEWAY_ID, request_id="req-x")
            )
        assert error.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


async def test_simulation_disabled_returns_failed_precondition(tmp_path: Path) -> None:
    spec = ServerSpec(
        server_id=GATEWAY_ID,
        device_id=DEVICE_ID,
        modbus_port=reserve_port(),
        slave_id=1,
        control_enabled=False,
    )
    gateway = await start_gateway_v1(
        tmp_path / "root-disabled",
        [spec],
        mtls={COMMANDER_CN: ["simulation_execute", "read"]},
        bootstrap_cn=COMMANDER_CN,
    )
    try:
        channel = gateway.mtls_channel(COMMANDER_CN)
        stub = gw_rpc.IndustrialGatewayServiceStub(channel)
        try:
            info = await stub.GetGateway(common.ObjectId(id=GATEWAY_ID))
            request = gw.SimulationCommandRequest(
                gateway_id=GATEWAY_ID,
                device_id=DEVICE_ID,
                point_id=COMMAND_FLOAT,
                value=encode_scalar(1500.0),
                request_id="req-disabled-1",
                expected_revision=info.gateway.revision,
                expires_at=_expires(),
            )
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await stub.ExecuteSimulationCommand(request)
            assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        finally:
            await channel.close()
    finally:
        await gateway.stop()
