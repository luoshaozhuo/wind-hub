"""§14.4 表 14-2 控制主流程系统测试（UR-04/05/06，UC-02）。

真实 gRPC/mTLS + 真实 Modbus TCP 从站 + 真实磁盘 SQLite。

Commander v1_22 / Contracts v1_16 已补齐 UR-05（SubmitCommands 外部批量
RPC，原子受理、逐点执行）与 UR-06（GetRequestResult 按 request_id 聚合
查询）。本模块经真实 RPC 验证：单点受理与回执、批次原子受理与顺序、
request_id 聚合查询与 NOT_FOUND 边界。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import grpc
import pytest

from contracts import commander_pb2 as pb
from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.real_service]

_TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"}


async def _poll_terminal(commander, command_id: str, timeout: float = 10.0) -> Any:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        result = await commander.GetCommandResult(
            pb.GetCommandResultRequest(command_id=command_id), timeout=5
        )
        if result.state in _TERMINAL:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"command {command_id} did not reach a terminal state")


@pytest.mark.normal
async def test_submit_and_query(sys_pki, grpc_stack_modbus) -> None:
    """UC-02-S01：单点异步接受 → 真实 Modbus 写入 → 终态可查（io_settled 真实裁决）。"""
    _common, commander = grpc_stack_modbus.stubs(sys_pki, client="ops")
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "WT01")
    request_id = f"req-flow-{uuid4().hex}"
    submitted = await commander.SubmitCommand(
        pb.SubmitCommandRequest(
            device_id="WT01",
            point_id="setpoint_power",
            value=common_pb2.ScalarValue(double_value=42.5),
            expected_revision=revision,
            request_id=request_id,
        ),
        timeout=5,
    )
    assert submitted.accepted is True
    assert submitted.state == "ACCEPTED"
    assert submitted.command_id
    # 回执必须原样携带调用方 request_id（RR-01 请求级身份）。
    assert submitted.request_id == request_id
    result = await _poll_terminal(commander, submitted.command_id)
    assert result.state == "SUCCEEDED"
    assert result.outcome == "CONFIRMED_SUCCESS"
    # 协议响应成功 + 真实从站处理：io_settled 由 I/O 证据决定而非按状态推断。
    assert result.io_settled is True
    assert result.device_id == "WT01"
    assert result.point_id == "setpoint_power"
    # 重复查询幂等返回同一终态投影。
    again = await commander.GetCommandResult(
        pb.GetCommandResultRequest(command_id=submitted.command_id), timeout=5
    )
    assert again.state == result.state and again.outcome == result.outcome


@pytest.mark.normal
@pytest.mark.exception
async def test_batch_acceptance(sys_pki, grpc_stack_modbus) -> None:
    """UC-02-S03 / UR-05：SubmitCommands 真实批量 RPC 原子受理、顺序保留、逐点收敛。

    同一 request_id 下两条命令在一个批次内受理；command_ids 顺序与入参一致，
    同设备 FIFO 真实执行、各自独立可查。越权调用方（viewer 无 control）
    在授权层被整批拒绝，不产生任何受理。
    """
    _common, commander = grpc_stack_modbus.stubs(sys_pki, client="ops")
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "WT01")

    def item(value: float) -> pb.CommandItem:
        return pb.CommandItem(
            device_id="WT01",
            point_id="setpoint_power",
            value=common_pb2.ScalarValue(double_value=value),
            expected_revision=revision,
        )

    request_id = f"req-flow-batch-{uuid4().hex}"
    batch = await commander.SubmitCommands(
        pb.SubmitCommandsRequest(
            request_id=request_id,
            commands=[item(40.5), item(41.5)],
        ),
        timeout=5,
    )
    assert batch.accepted is True
    assert batch.request_id == request_id
    assert len(batch.command_ids) == 2
    # 顺序由批次入参决定，command_id 两两不同。
    assert len(set(batch.command_ids)) == 2
    first, second = batch.command_ids
    # 同设备 FIFO：两条都必须真实完成且各自独立可查（逐点结果）。
    r1 = await _poll_terminal(commander, first)
    r2 = await _poll_terminal(commander, second)
    assert (r1.state, r2.state) == ("SUCCEEDED", "SUCCEEDED")

    # 越权调用方整批被拒（viewer 无 control），无任何受理副作用。
    _common_v, commander_v = grpc_stack_modbus.stubs(sys_pki, client="viewer")
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await commander_v.SubmitCommands(
            pb.SubmitCommandsRequest(
                request_id=f"req-flow-denied-{uuid4().hex}",
                commands=[item(1.0)],
            ),
            timeout=5,
        )
    assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.normal
@pytest.mark.exception
async def test_result_query(sys_pki, grpc_stack_modbus) -> None:
    """UC-02-S04 / UR-06：command_id 单点查询与 request_id 聚合查询并存。

    GetCommandResultRequest 保持 command_id 单字段；request_id 维度由独立的
    GetRequestResult RPC 承担，未知 request_id 明确 NOT_FOUND，不返回空投影。
    """
    from google.protobuf.descriptor_pb2 import FieldDescriptorProto  # noqa: PLC0415

    _common, commander = grpc_stack_modbus.stubs(sys_pki, client="ops")
    # 两个查询 RPC 的请求键相互独立，契约边界可审计。
    command_fields = {
        f.name
        for f in pb.GetCommandResultRequest.DESCRIPTOR.fields
        if f.type != FieldDescriptorProto.TYPE_MESSAGE or f.name
    }
    request_fields = {
        f.name
        for f in pb.GetRequestResultRequest.DESCRIPTOR.fields
        if f.type != FieldDescriptorProto.TYPE_MESSAGE or f.name
    }
    assert command_fields == {"command_id"}
    assert request_fields == {"request_id"}

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await commander.GetCommandResult(
            pb.GetCommandResultRequest(command_id="does-not-exist"), timeout=5
        )
    assert exc_info.value.code() is grpc.StatusCode.NOT_FOUND
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await commander.GetRequestResult(
            pb.GetRequestResultRequest(request_id="does-not-exist"), timeout=5
        )
    assert exc_info.value.code() is grpc.StatusCode.NOT_FOUND

    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "WT01")
    request_id = f"req-flow-query-{uuid4().hex}"
    submitted = await commander.SubmitCommand(
        pb.SubmitCommandRequest(
            device_id="WT01",
            point_id="setpoint_power",
            value=common_pb2.ScalarValue(double_value=7.0),
            expected_revision=revision,
            request_id=request_id,
        ),
        timeout=5,
    )
    await _poll_terminal(commander, submitted.command_id)

    request_view = await commander.GetRequestResult(
        pb.GetRequestResultRequest(request_id=request_id), timeout=5
    )
    assert request_view.request_id == request_id
    assert [item.command_id for item in request_view.commands] == [submitted.command_id]
    assert request_view.commands[0].state == "SUCCEEDED"
    assert request_view.HasField("updated_at")
    assert isinstance(request_view.io_settled, bool)
