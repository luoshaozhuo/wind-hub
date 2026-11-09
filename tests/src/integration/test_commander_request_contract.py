"""请求级契约真实 gRPC 端到端测试（Commander v1_22 RR-01/RR-02，Contracts v1_16）。

链路全部真实：mTLS（EC P-256 PKI、require_client_auth）+ build_commander
组合根 + 磁盘 SQLite（WAL/FULL）+ 真实 Modbus TCP 从站（pymodbus），
不以内存替身冒充任何一段。

覆盖任务书语义项：
- ① 空白 request_id / 空批次在 gRPC 边界 INVALID_ARGUMENT；
- ② 同 caller 同 request_id 重放返回同 command_id 且现场只写一次；异内容
  ALREADY_EXISTS；
- ④ SubmitCommands 原子受理、command_ids 顺序保留、同设备 FIFO 收敛；
- ⑤ 批次内任一非法子项整批拒绝，零受理、零设备 I/O；
- ⑥ 客户端截止期（响应丢失语义）不导致重复现场写，shield 受理仍可按
  request_id 回查且重放幂等；
- ⑩ GetRequestResult 聚合状态、子命令顺序、io_settled 与 NOT_FOUND 边界。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import grpc
import pytest

from contracts import commander_pb2 as pb
from contracts import common_pb2
from tests.commander.grpc_harness import GrpcStack, Pki, build_pki, build_stack
from tests.fixtures.servers.modbus_server import ModbusMockServer

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

_TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED", "EXPIRED", "CANCELLED"}


@pytest.fixture
async def modbus_grpc_stack(
    tmp_path, modbus_server: ModbusMockServer
) -> AsyncGenerator[tuple[GrpcStack, Any, Pki, ModbusMockServer], None]:
    pki = build_pki(tmp_path)
    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,
        label="src-request-contract",
        stream_prefix="CMD_SRC_RC",
    )
    try:
        yield stack, server, pki, modbus_server
    finally:
        await server.stop(grace=5)
        await service.stop()


async def _revision(commander: Any) -> str:
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    return str(next(device.revision for device in devices.devices if device.device_id == "WT01"))


def _single_request(revision: str, request_id: str, *, value: float = 42.5) -> Any:
    return pb.SubmitCommandRequest(
        device_id="WT01",
        point_id="setpoint_power",
        value=common_pb2.ScalarValue(double_value=value),
        expected_revision=revision,
        request_id=request_id,
    )


def _batch_item(revision: str, value: float) -> pb.CommandItem:
    return pb.CommandItem(
        device_id="WT01",
        point_id="setpoint_power",
        value=common_pb2.ScalarValue(double_value=value),
        expected_revision=revision,
    )


async def _poll_request(commander: Any, request_id: str, *, timeout: float = 10.0) -> Any | None:
    """按 request_id 轮询聚合视图；NOT_FOUND 持续到超时则返回 None。"""
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        try:
            return await commander.GetRequestResult(
                pb.GetRequestResultRequest(request_id=request_id), timeout=5
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() is not grpc.StatusCode.NOT_FOUND:
                raise
        await asyncio.sleep(0.05)
    return None


async def _poll_request_terminal(commander: Any, request_id: str) -> Any:
    deadline = datetime.now(UTC) + timedelta(seconds=10.0)
    while datetime.now(UTC) < deadline:
        view = await _poll_request(commander, request_id, timeout=5.0)
        if view is not None and view.state in _TERMINAL:
            return view
        await asyncio.sleep(0.05)
    raise AssertionError(f"request {request_id} never reached a terminal state")


async def test_replay_same_request_id_returns_same_command_without_second_write(
    modbus_grpc_stack: tuple[GrpcStack, Any, Pki, ModbusMockServer],
) -> None:
    """语义②：同 caller + 同 request_id + 同载荷重放，command_id 不变且零重复现场写。"""
    stack, _server, pki, modbus = modbus_grpc_stack
    _common, commander = stack.stubs(pki, client="ops")
    revision = await _revision(commander)
    request_id = f"req-rc-replay-{uuid4().hex}"

    submitted = await commander.SubmitCommand(_single_request(revision, request_id), timeout=5)
    await _poll_request_terminal(commander, request_id)
    writes_after_first = modbus.write_count
    assert writes_after_first >= 1

    replay = await commander.SubmitCommand(_single_request(revision, request_id), timeout=5)
    # 幂等受理回执：同一 command_id、同一 request_id，不产生新的受理事件。
    assert replay.command_id == submitted.command_id
    assert replay.request_id == request_id
    assert modbus.write_count == writes_after_first


async def test_same_request_id_different_payload_rejected_already_exists(
    modbus_grpc_stack: tuple[GrpcStack, Any, Pki, ModbusMockServer],
) -> None:
    """语义②：同 caller + 同 request_id + 异值 → ALREADY_EXISTS，原命令与现场写均不变。"""
    stack, _server, pki, modbus = modbus_grpc_stack
    _common, commander = stack.stubs(pki, client="ops")
    revision = await _revision(commander)
    request_id = f"req-rc-conflict-{uuid4().hex}"

    submitted = await commander.SubmitCommand(
        _single_request(revision, request_id, value=42.5), timeout=5
    )
    await _poll_request_terminal(commander, request_id)
    writes = modbus.write_count

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await commander.SubmitCommand(_single_request(revision, request_id, value=99.9), timeout=5)
    assert exc_info.value.code() is grpc.StatusCode.ALREADY_EXISTS
    # 冲突拒绝不增加任何现场写入，原请求聚合视图仍指向原命令。
    assert modbus.write_count == writes
    view = await commander.GetRequestResult(
        pb.GetRequestResultRequest(request_id=request_id), timeout=5
    )
    assert [item.command_id for item in view.commands] == [submitted.command_id]


async def test_submit_commands_atomic_order_and_fifo_settlement(
    modbus_grpc_stack: tuple[GrpcStack, Any, Pki, ModbusMockServer],
) -> None:
    """语义④：批量 RPC 原子受理，command_ids 顺序等同入参，同设备 FIFO 逐点收敛。"""
    stack, _server, pki, modbus = modbus_grpc_stack
    _common, commander = stack.stubs(pki, client="ops")
    revision = await _revision(commander)
    request_id = f"req-rc-batch-{uuid4().hex}"
    before = modbus.write_count

    batch = await commander.SubmitCommands(
        pb.SubmitCommandsRequest(
            request_id=request_id,
            commands=[_batch_item(revision, 40.5), _batch_item(revision, 41.5)],
        ),
        timeout=5,
    )
    assert batch.accepted is True
    assert batch.request_id == request_id
    assert len(batch.command_ids) == 2
    assert len(set(batch.command_ids)) == 2

    view = await _poll_request_terminal(commander, request_id)
    # 聚合视图的子命令顺序与受理回执一致，两点均真实成功。
    assert [item.command_id for item in view.commands] == list(batch.command_ids)
    assert [item.state for item in view.commands] == ["SUCCEEDED", "SUCCEEDED"]
    assert view.state == "SUCCEEDED"
    assert view.io_settled is True
    assert modbus.write_count - before == 2

    # 整批重放：同 request_id 同指纹，回执 command_id 序列完全一致且无新增现场写。
    replay = await commander.SubmitCommands(
        pb.SubmitCommandsRequest(
            request_id=request_id,
            commands=[_batch_item(revision, 40.5), _batch_item(revision, 41.5)],
        ),
        timeout=5,
    )
    assert list(replay.command_ids) == list(batch.command_ids)
    assert modbus.write_count - before == 2


async def test_batch_with_one_invalid_item_rejected_entirely_without_field_io(
    modbus_grpc_stack: tuple[GrpcStack, Any, Pki, ModbusMockServer],
) -> None:
    """语义⑤：任一子项缺 point_id，整批 INVALID_ARGUMENT；无受理、无设备 I/O。"""
    stack, _server, pki, modbus = modbus_grpc_stack
    _common, commander = stack.stubs(pki, client="ops")
    revision = await _revision(commander)
    request_id = f"req-rc-invalid-{uuid4().hex}"
    before = modbus.write_count

    broken = pb.CommandItem(
        device_id="WT01",
        point_id="",  # 非法子项：空 point_id 在适配层即被拒。
        value=common_pb2.ScalarValue(double_value=1.0),
        expected_revision=revision,
    )
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await commander.SubmitCommands(
            pb.SubmitCommandsRequest(
                request_id=request_id,
                commands=[_batch_item(revision, 1.0), broken],
            ),
            timeout=5,
        )
    assert exc_info.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    # 整批无一受理：request_id 维度永久 NOT_FOUND，Modbus 从站零写入。
    assert await _poll_request(commander, request_id, timeout=1.0) is None
    assert modbus.write_count == before

    # 空批次同样在边界被拒。
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await commander.SubmitCommands(
            pb.SubmitCommandsRequest(request_id=f"req-rc-empty-{uuid4().hex}"),
            timeout=5,
        )
    assert exc_info.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert modbus.write_count == before


async def test_get_request_result_aggregate_and_not_found(
    modbus_grpc_stack: tuple[GrpcStack, Any, Pki, ModbusMockServer],
) -> None:
    """语义⑩：聚合视图字段完整；未知 request_id 明确 NOT_FOUND 而非空投影。"""
    stack, _server, pki, _modbus = modbus_grpc_stack
    _common, commander = stack.stubs(pki, client="ops")

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await commander.GetRequestResult(
            pb.GetRequestResultRequest(request_id=f"req-rc-missing-{uuid4().hex}"),
            timeout=5,
        )
    assert exc_info.value.code() is grpc.StatusCode.NOT_FOUND

    revision = await _revision(commander)
    request_id = f"req-rc-agg-{uuid4().hex}"
    submitted = await commander.SubmitCommand(
        _single_request(revision, request_id, value=7.25), timeout=5
    )
    view = await _poll_request_terminal(commander, request_id)
    assert view.request_id == request_id
    assert [item.command_id for item in view.commands] == [submitted.command_id]
    assert view.commands[0].state == "SUCCEEDED"
    assert view.commands[0].io_settled is True
    assert view.HasField("updated_at")


async def test_blank_request_id_rejected_invalid_argument(
    modbus_grpc_stack: tuple[GrpcStack, Any, Pki, ModbusMockServer],
) -> None:
    """语义①：空白 request_id 在 gRPC 边界拒绝，不进入受理流程。"""
    stack, _server, pki, modbus = modbus_grpc_stack
    _common, commander = stack.stubs(pki, client="ops")
    revision = await _revision(commander)
    before = modbus.write_count

    for blank in ("", "   "):
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await commander.SubmitCommand(_single_request(revision, blank), timeout=5)
        assert exc_info.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert modbus.write_count == before


async def test_rpc_deadline_does_not_duplicate_field_write(
    modbus_grpc_stack: tuple[GrpcStack, Any, Pki, ModbusMockServer],
) -> None:
    """语义⑥：客户端极短截止期丢失响应，shield 受理不中止；每请求至多一次现场写。

    截止期可能在请求送达前到期（此时服务端无受理，换新 ID 重试），也可能在
    受理窗口内到期（此时 shield 保护的受理继续落定）。只有后一种情形提供
    取消语义证据；若连续三次都未送达，本测试显式失败，绝不静默放过。
    """
    stack, _server, pki, modbus = modbus_grpc_stack
    _common, commander = stack.stubs(pki, client="ops")
    revision = await _revision(commander)

    demonstrated = False
    for attempt in range(3):
        request_id = f"req-rc-deadline-{uuid4().hex}"
        before = modbus.write_count
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await commander.SubmitCommand(
                _single_request(revision, request_id, value=50.0 + attempt),
                timeout=0.05,
            )
        assert exc_info.value.code() in (
            grpc.StatusCode.DEADLINE_EXCEEDED,
            grpc.StatusCode.CANCELLED,
        )
        view = await _poll_request(commander, request_id, timeout=5.0)
        if view is None:
            # 截止期先于请求送达：本试次无受理、无现场写，换新 ID 重试。
            assert modbus.write_count == before
            continue

        demonstrated = True
        terminal = await _poll_request_terminal(commander, request_id)
        assert terminal.state == "SUCCEEDED"
        # 核心不变量：一次请求只允许一次现场写入，客户端超时不触发重发。
        assert modbus.write_count - before == 1
        # 同 ID 重放拿回同一 command_id，且不产生第二次写入。
        replay = await commander.SubmitCommand(
            _single_request(revision, request_id, value=50.0 + attempt), timeout=5
        )
        assert replay.command_id == terminal.commands[0].command_id
        assert modbus.write_count - before == 1
        break

    assert demonstrated, "三个试次均在请求送达前超时，未取得取消语义证据"
