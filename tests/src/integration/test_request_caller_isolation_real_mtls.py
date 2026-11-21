"""语义⑪：gRPC/Bearer 边界的调用方命名空间隔离（真实链路）。

两个独立 Bearer 主体均持 control/query 授权，使用同一 request_id 提交同一
载荷时，服务端必须按认证主体映射出两个 caller 命名空间；查询、重放和真实
Modbus 写入次数均保持隔离。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from contracts import commander_pb2 as pb
from contracts import commander_pb2_grpc, common_pb2
from tests.commander.grpc_harness import GrpcStack, build_pki, build_stack
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.bearer import bearer_channel

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

_SECOND_CALLER_ID = "ops2-console"


def _second_stub(stack: GrpcStack) -> Any:
    return commander_pb2_grpc.CommanderServiceStub(bearer_channel(stack.target, "ops2"))


async def _revision(commander: Any) -> str:
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    return str(next(d.revision for d in devices.devices if d.device_id == "WT01"))


def _request(revision: str, request_id: str) -> pb.SubmitCommandRequest:
    return pb.SubmitCommandRequest(
        device_id="WT01",
        point_id="setpoint_power",
        value=common_pb2.ScalarValue(double_value=42.5),
        expected_revision=revision,
        request_id=request_id,
    )


async def test_same_request_id_isolated_across_bearer_callers(
    tmp_path, modbus_server: ModbusMockServer
) -> None:
    pki = build_pki(tmp_path)
    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,
        label="src-caller-isolation",
        stream_prefix="CMD_SRC_ISO",
        extra_principals={"ops2": (_SECOND_CALLER_ID, ("control", "query"))},
    )
    try:
        _common, commander_a = stack.stubs(pki, client="ops")
        commander_b = _second_stub(stack)

        revision = await _revision(commander_a)
        request_id = f"req-iso-{uuid4().hex}"
        before = modbus_server.write_count

        first = await commander_a.SubmitCommand(_request(revision, request_id), timeout=5)
        second = await commander_b.SubmitCommand(_request(revision, request_id), timeout=5)
        assert first.command_id != second.command_id
        assert first.request_id == request_id == second.request_id

        replay_a = await commander_a.SubmitCommand(_request(revision, request_id), timeout=5)
        replay_b = await commander_b.SubmitCommand(_request(revision, request_id), timeout=5)
        assert replay_a.command_id == first.command_id
        assert replay_b.command_id == second.command_id

        terminal = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED", "EXPIRED", "CANCELLED"}
        view_a = None
        view_b = None
        for _ in range(200):
            view_a = await commander_a.GetRequestResult(
                pb.GetRequestResultRequest(request_id=request_id), timeout=5
            )
            view_b = await commander_b.GetRequestResult(
                pb.GetRequestResultRequest(request_id=request_id), timeout=5
            )
            if view_a.state in terminal and view_b.state in terminal:
                break
            await asyncio.sleep(0.05)
        assert view_a is not None and view_a.state == "SUCCEEDED"
        assert view_b is not None and view_b.state == "SUCCEEDED"
        assert [item.command_id for item in view_a.commands] == [first.command_id]
        assert [item.command_id for item in view_b.commands] == [second.command_id]
        assert modbus_server.write_count - before == 2
    finally:
        await server.stop(grace=5)
        await service.stop()
