"""§14.4 表 14-2 控制主流程系统测试（UR-04/05/06，UC-02）。

真实 gRPC/mTLS + 真实 Modbus TCP 从站 + 真实磁盘 SQLite。

Contracts GAP（§14.1/§14.4 明示）：commander.proto 当前没有外部批量提交
RPC（UR-05）与按 request_id 外部查询 RPC（UR-06）。test_batch_acceptance
仅以应用层多次单点 RPC 验证批量场景的原子受理与逐点结果，不得计作
UR-05 对外 RPC PASS；test_result_query 同样不宣称 request_id 覆盖。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import grpc
import pytest

from contracts import commander_pb2 as pb
from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.real_service]

_TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"}


async def _poll_terminal(commander, command_id: str, timeout: float = 10.0) -> object:
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
    submitted = await commander.SubmitCommand(
        pb.SubmitCommandRequest(
            device_id="WT01",
            point_id="setpoint_power",
            value=common_pb2.ScalarValue(double_value=42.5),
            expected_revision=revision,
        ),
        timeout=5,
    )
    assert submitted.accepted is True
    assert submitted.state == "ACCEPTED"
    assert submitted.command_id
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
    """UC-02-S03 应用层批量：FIFO 串行两条 + 第三条越权不影响已受理命令。

    UR-05 对外批量 RPC 为 Contracts GAP：本测试不经过任何批量 RPC，
    仅验证应用层在真实协议链路上的逐点结果语义。
    """
    _common, commander = grpc_stack_modbus.stubs(sys_pki, client="ops")
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "WT01")

    async def submit(value: float) -> str:
        response = await commander.SubmitCommand(
            pb.SubmitCommandRequest(
                device_id="WT01",
                point_id="setpoint_power",
                value=common_pb2.ScalarValue(double_value=value),
                expected_revision=revision,
            ),
            timeout=5,
        )
        assert response.accepted is True
        return response.command_id

    first = await submit(40.5)
    second = await submit(41.5)
    # 同设备 FIFO：两条都必须真实完成且各自独立可查（逐点结果）。
    r1 = await _poll_terminal(commander, first)
    r2 = await _poll_terminal(commander, second)
    assert (r1.state, r2.state) == ("SUCCEEDED", "SUCCEEDED")
    assert r1.command_id != r2.command_id

    # 越权第三条被拒（viewer 无 control），前两条结果不受影响。
    _common_v, commander_v = grpc_stack_modbus.stubs(sys_pki, client="viewer")
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await commander_v.SubmitCommand(
            pb.SubmitCommandRequest(
                device_id="WT01",
                point_id="setpoint_power",
                value=common_pb2.ScalarValue(double_value=1.0),
                expected_revision=revision,
            ),
            timeout=5,
        )
    assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.normal
@pytest.mark.exception
async def test_result_query(sys_pki, grpc_stack_modbus) -> None:
    """UC-02-S04：按 command_id 查 state/outcome/io_settled；不存在明确 NOT_FOUND。

    UR-06：按 request_id 的对外查询 RPC 在 commander.proto 中不存在，
    属 Contracts GAP；本测试不宣称 request_id 外部查询覆盖。
    """
    from google.protobuf.descriptor_pb2 import FieldDescriptorProto  # noqa: PLC0415

    _common, commander = grpc_stack_modbus.stubs(sys_pki, client="ops")
    # 契约缺口可审计：GetCommandResultRequest 只有 command_id 字段。
    fields = {
        f.name
        for f in pb.GetCommandResultRequest.DESCRIPTOR.fields
        if f.type != FieldDescriptorProto.TYPE_MESSAGE or f.name
    }
    assert "command_id" in fields and "request_id" not in fields

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await commander.GetCommandResult(
            pb.GetCommandResultRequest(command_id="does-not-exist"), timeout=5
        )
    assert exc_info.value.code() is grpc.StatusCode.NOT_FOUND

    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "WT01")
    submitted = await commander.SubmitCommand(
        pb.SubmitCommandRequest(
            device_id="WT01",
            point_id="setpoint_power",
            value=common_pb2.ScalarValue(double_value=7.0),
            expected_revision=revision,
        ),
        timeout=5,
    )
    result = await _poll_terminal(commander, submitted.command_id)
    assert result.HasField("updated_at")
    assert result.state == "SUCCEEDED"
    assert isinstance(result.io_settled, bool)
