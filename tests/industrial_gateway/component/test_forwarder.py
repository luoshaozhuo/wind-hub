"""CommandForwarder ↔ 真实 grpc.aio Commander 组件契约。

真实 gRPC 对端（grpc.aio.server + CommanderServiceServicer），无 Mock；
覆盖 v1 尽力而为转发的全部终态语义：ACCEPTED / REJECTED / UNKNOWN /
OVERLOADED、队列边界、提交期限、同目标串行、至多一次（无自动重试）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import grpc
import pytest

from contracts import commander_pb2_grpc as crpc
from industrial_gateway.infrastructure.commander.forwarder import (
    CommandForwarder,
    ForwardState,
)
from industrial_gateway.infrastructure.config.v1_schema import ControlConfig
from tests.support.gateway_v1 import FakeCommanderService

pytestmark = [pytest.mark.component, pytest.mark.real_service]


async def _pair(
    tmp: Path,
    *,
    capacity: int = 1024,
    inflight: int = 16,
    deadline_ms: int = 5000,
    commander: FakeCommanderService | None = None,
) -> tuple[CommandForwarder, FakeCommanderService, grpc.aio.Server]:
    commander = commander or FakeCommanderService()
    server = grpc.aio.server()
    crpc.add_CommanderServiceServicer_to_server(commander, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    forwarder = CommandForwarder(
        ControlConfig(
            commander_endpoint=f"127.0.0.1:{port}",
            forward_queue_capacity=capacity,
            max_forward_inflight=inflight,
            submit_deadline_ms=deadline_ms,
        ),
        project_root=tmp,
    )
    await forwarder.start()
    return forwarder, commander, server


async def _terminal(forwarder: CommandForwarder, event_id: str, *, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    pending = {
        ForwardState.QUEUED,
        ForwardState.VALIDATING,
        ForwardState.SUBMITTING,
    }
    while asyncio.get_running_loop().time() < deadline:
        record = next(item for item in forwarder.records if item.event_id == event_id)
        if record.state not in pending:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"event {event_id} did not terminate: {forwarder.records}")


async def test_accepted_same_value_twice_with_distinct_command_ids(tmp_path: Path) -> None:
    forwarder, commander, server = await _pair(tmp_path)
    try:
        ids: list[str] = []
        for _ in range(2):
            ids.append(f"evt-{len(ids)}")
            assert forwarder.capture(
                event_id=ids[-1],
                gateway_id="gw1",
                device_id="D01",
                point_id="cmd_uint",
                value=42,
                revision="config-rev",
            )
        records = [await _terminal(forwarder, event_id) for event_id in ids]
        assert [record.state for record in records] == [
            ForwardState.ACCEPTED,
            ForwardState.ACCEPTED,
        ]
        assert len(commander.calls) == 2
        assert commander.calls[0].command_id != commander.calls[1].command_id
        # 提交时使用 Commander GetDevice 的 revision，而非 capture 传入的配置修订。
        assert {call.revision for call in commander.calls} == {"rev-D01"}
        assert {record.revision for record in records} == {"config-rev"}
        assert commander.get_device_calls.count("D01") == 1  # 1s 缓存
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


async def test_rejected_is_distinct_from_unknown_and_not_retried(tmp_path: Path) -> None:
    commander = FakeCommanderService()
    commander.set_rejected("D01", "cmd_uint", message="interlock open")
    forwarder, _, server = await _pair(tmp_path, commander=commander)
    try:
        assert forwarder.capture(
            event_id="evt-r",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=1,
            revision="rev-config",
        )
        record = await _terminal(forwarder, "evt-r")
        assert record.state is ForwardState.REJECTED
        assert "interlock open" in record.error
        await asyncio.sleep(0.2)
        assert len(commander.calls) == 1  # 无自动重试
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


async def test_submission_rpc_error_is_unknown_not_success(tmp_path: Path) -> None:
    """SUBMITTING 阶段 Commander 返回 RpcError：只能判 UNKNOWN，不得报成功。"""
    commander = FakeCommanderService()
    commander.rpc_aborts[("D01", "cmd_uint")] = grpc.StatusCode.UNAVAILABLE
    forwarder, _, server = await _pair(tmp_path, commander=commander)
    try:
        assert forwarder.capture(
            event_id="evt-u",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=1,
            revision="rev",
        )
        record = await _terminal(forwarder, "evt-u")
        assert record.state is ForwardState.UNKNOWN
        assert not record.command_id
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


@pytest.mark.parametrize(
    "code",
    [
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.OUT_OF_RANGE,
        grpc.StatusCode.UNIMPLEMENTED,
    ],
)
async def test_authoritative_rpc_refusal_is_rejected_not_unknown(
    tmp_path: Path, code: grpc.StatusCode
) -> None:
    """FWD-SEM-01：七类状态码在 RPC 边界明确拒绝执行 → REJECTED，不是 UNKNOWN。

    这些状态码都能证明 Commander 未执行命令（参数/鉴权/前置条件/路由/能力检查
    发生在执行之前）。真实 grpc.aio 对端 abort，无 Mock。
    """
    commander = FakeCommanderService()
    commander.rpc_aborts[("D01", "cmd_uint")] = code
    forwarder, _, server = await _pair(tmp_path, commander=commander)
    try:
        forwarder.capture(
            event_id="evt-authoritative",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=1,
            revision="rev",
        )
        record = await _terminal(forwarder, "evt-authoritative")
        assert record.state is ForwardState.REJECTED
        assert not record.command_id
        # 真实入站恰好一次，无自动重试；Commander 侧未生成任何命令。
        assert commander.submit_attempts[("D01", "cmd_uint")] == 1
        assert commander.calls == []
        await asyncio.sleep(0.2)
        assert commander.submit_attempts[("D01", "cmd_uint")] == 1

        # REJECTED 不闭锁：清除故障后新事件正常 ACCEPTED，且因缓存失效重新
        # 查询了一次 GetDevice（权威拒绝会弹出 revision 缓存）。
        commander.rpc_aborts.clear()
        forwarder.capture(
            event_id="evt-recovered",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=2,
            revision="rev",
        )
        recovered = await _terminal(forwarder, "evt-recovered")
        assert recovered.state is ForwardState.ACCEPTED
        assert recovered.command_id
        assert commander.get_device_calls.count("D01") == 2
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


@pytest.mark.parametrize(
    "code",
    [
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.CANCELLED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.ABORTED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
    ],
)
async def test_settlement_uncertain_rpc_failure_stays_unknown(
    tmp_path: Path, code: grpc.StatusCode
) -> None:
    """传输/超时/取消/服务内部错误不能证明命令未执行 → 必须保持 UNKNOWN。

    同时验证缓存不被弹出（不确定性失败不否定已取得的设备 revision）。
    """
    commander = FakeCommanderService()
    commander.rpc_aborts[("D01", "cmd_uint")] = code
    forwarder, _, server = await _pair(tmp_path, commander=commander)
    try:
        forwarder.capture(
            event_id="evt-uncertain",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=1,
            revision="rev",
        )
        record = await _terminal(forwarder, "evt-uncertain")
        assert record.state is ForwardState.UNKNOWN
        assert not record.command_id
        assert commander.submit_attempts[("D01", "cmd_uint")] == 1  # 无重试
        assert commander.calls == []  # abort 在记录前发生；是否已执行天然不确定

        # 缓存保留：恢复后 1s TTL 内的新事件不重新查询 GetDevice。
        commander.rpc_aborts.clear()
        forwarder.capture(
            event_id="evt-after",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=2,
            revision="rev",
        )
        after = await _terminal(forwarder, "evt-after")
        assert after.state is ForwardState.ACCEPTED
        assert commander.get_device_calls.count("D01") == 1
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


async def test_accepted_without_command_id_is_unknown(tmp_path: Path) -> None:
    """accepted=True 但缺 command_id：无权威回执，必须 UNKNOWN。"""
    commander = FakeCommanderService()
    commander.accepted_without_id.add(("D01", "cmd_uint"))
    forwarder, _, server = await _pair(tmp_path, commander=commander)
    try:
        forwarder.capture(
            event_id="evt-no-id",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=1,
            revision="rev",
        )
        record = await _terminal(forwarder, "evt-no-id")
        assert record.state is ForwardState.UNKNOWN
        assert not record.command_id
        assert commander.submit_attempts[("D01", "cmd_uint")] == 1  # 无重试
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


async def test_authoritative_refusal_isolated_per_device_and_point(
    tmp_path: Path,
) -> None:
    """D01/cmd_uint 的权威拒绝不得影响 D02 同点与其他提交路径。"""
    commander = FakeCommanderService()
    commander.rpc_aborts[("D01", "cmd_uint")] = grpc.StatusCode.FAILED_PRECONDITION
    forwarder, _, server = await _pair(tmp_path, inflight=8, commander=commander)
    try:
        forwarder.capture(
            event_id="evt-bad",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=1,
            revision="rev",
        )
        forwarder.capture(
            event_id="evt-ok",
            gateway_id="gw2",
            device_id="D02",
            point_id="cmd_uint",
            value=9,
            revision="rev",
        )
        records = {
            event_id: await _terminal(forwarder, event_id) for event_id in ("evt-bad", "evt-ok")
        }
        assert records["evt-bad"].state is ForwardState.REJECTED
        assert records["evt-ok"].state is ForwardState.ACCEPTED
        assert commander.submit_attempts[("D01", "cmd_uint")] == 1
        assert commander.submit_attempts[("D02", "cmd_uint")] == 1
        assert [call.device_id for call in commander.calls] == ["D02"]
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


async def test_pre_submit_failure_is_rejected_not_unknown(tmp_path: Path) -> None:
    """GetDevice 失败发生在提交之前：确定未送达现场，必须是 REJECTED。"""
    commander = FakeCommanderService()

    async def abort_get(device_id: str, context: grpc.aio.ServicerContext):
        await context.abort(grpc.StatusCode.NOT_FOUND, "unknown device")

    commander.get_device_hook = abort_get
    forwarder, _, server = await _pair(tmp_path, commander=commander)
    try:
        forwarder.capture(
            event_id="evt-pre",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=1,
            revision="rev",
        )
        record = await _terminal(forwarder, "evt-pre")
        assert record.state is ForwardState.REJECTED
        assert "revision unavailable" in record.error
        assert commander.calls == []  # 从未提交
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


async def test_queue_overflow_reports_overloaded(tmp_path: Path) -> None:
    commander = FakeCommanderService()
    commander.set_delay("D01", "cmd_uint", 0.3)
    forwarder, _, server = await _pair(tmp_path, capacity=2, inflight=1, commander=commander)
    try:
        outcomes: list[bool] = []
        for index in range(6):
            outcomes.append(
                forwarder.capture(
                    event_id=f"evt-{index}",
                    gateway_id="gw1",
                    device_id="D01",
                    point_id="cmd_uint",
                    value=index,
                    revision="rev",
                )
            )
        queued = sum(outcomes)
        assert queued <= 2
        records = list(forwarder.records)
        assert any(record.state is ForwardState.OVERLOADED for record in records)
        overloaded = next(record for record in records if record.state is ForwardState.OVERLOADED)
        assert "queue unavailable" in overloaded.error
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


async def test_same_target_serialized_other_targets_concurrent(tmp_path: Path) -> None:
    commander = FakeCommanderService()
    commander.set_delay("D01", "cmd_uint", 0.4)
    commander.set_delay("D02", "cmd_uint", 0.4)
    forwarder, _, server = await _pair(tmp_path, inflight=8, commander=commander)
    try:
        for index in range(3):
            forwarder.capture(
                event_id=f"a-{index}",
                gateway_id="gw1",
                device_id="D01",
                point_id="cmd_uint",
                value=index,
                revision="rev",
            )
        forwarder.capture(
            event_id="b-0",
            gateway_id="gw2",
            device_id="D02",
            point_id="cmd_uint",
            value=9,
            revision="rev",
        )
        await asyncio.gather(*(_terminal(forwarder, name) for name in ("a-0", "a-1", "a-2", "b-0")))
        assert commander.max_inflight("D01", "cmd_uint") == 1
        assert commander.calls  # D02 不必等 D01 队列排空
        d02 = [call for call in commander.calls if call.device_id == "D02"]
        assert len(d02) == 1
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


async def test_expired_while_queued_never_submits(tmp_path: Path) -> None:
    """队列中过期：单 worker 被长在途占住，第二事件在队列中熬过自身截止期，
    出队后必须直接 REJECTED("queue entry expired")，不查 revision、不提交。"""
    commander = FakeCommanderService()
    commander.set_delay("D01", "cmd_uint", 0.6)
    forwarder, _, server = await _pair(tmp_path, inflight=1, deadline_ms=300, commander=commander)
    try:
        forwarder.capture(
            event_id="evt-holder",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=1,
            revision="rev",
        )
        forwarder.capture(
            event_id="evt-exp",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=2,
            revision="rev",
        )
        record = await _terminal(forwarder, "evt-exp", timeout=5.0)
        assert record.state is ForwardState.REJECTED
        assert record.error == "queue entry expired"
        # 只有占锁事件真正提交；过期事件未发起第二次 SubmitCommand。
        await asyncio.sleep(0.1)
        assert len(commander.calls) == 1
        assert commander.get_device_calls.count("D01") == 1
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


async def test_lookup_timeout_before_submit_is_rejected(tmp_path: Path) -> None:
    """GetDevice 在提交期限内未返回：确定未送达现场，REJECTED 而非 UNKNOWN。"""
    commander = FakeCommanderService()

    async def slow_get(device_id: str, context: grpc.aio.ServicerContext):
        await asyncio.sleep(0.4)
        from contracts import commander_pb2 as cpb

        return cpb.GetDeviceResponse(device=cpb.Device(device_id=device_id, revision="late"))

    commander.get_device_hook = slow_get
    forwarder, _, server = await _pair(tmp_path, inflight=1, deadline_ms=200, commander=commander)
    try:
        forwarder.capture(
            event_id="evt-lookup",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=1,
            revision="rev",
        )
        record = await _terminal(forwarder, "evt-lookup")
        assert record.state is ForwardState.REJECTED
        assert "device revision unavailable" in record.error
        assert commander.calls == []  # 从未提交，不得误判 UNKNOWN
    finally:
        await forwarder.stop()
        await server.stop(grace=0)


async def test_stop_marks_inflight_unknown_and_drains_queued_rejected(
    tmp_path: Path,
) -> None:
    commander = FakeCommanderService()
    commander.set_delay("D01", "cmd_uint", 0.5)
    forwarder, _, server = await _pair(tmp_path, capacity=8, inflight=2, commander=commander)
    # 一个在途 + 一个排队（同目标锁保证第二个排队）。
    forwarder.capture(
        event_id="evt-inflight",
        gateway_id="gw1",
        device_id="D01",
        point_id="cmd_uint",
        value=1,
        revision="rev",
    )
    await asyncio.sleep(0.1)
    forwarder.capture(
        event_id="evt-queued",
        gateway_id="gw1",
        device_id="D01",
        point_id="cmd_uint",
        value=2,
        revision="rev",
    )
    await asyncio.sleep(0.1)
    await forwarder.stop()
    states = {record.event_id: record.state for record in forwarder.records}
    assert states["evt-inflight"] in {ForwardState.UNKNOWN, ForwardState.ACCEPTED}
    assert states["evt-queued"] is ForwardState.REJECTED
    await server.stop(grace=0)
    # 停止后再捕获直接 OVERLOADED，不进入队列。
    assert not forwarder.capture(
        event_id="dead",
        gateway_id="gw1",
        device_id="D01",
        point_id="cmd_uint",
        value=1,
        revision="rev",
    )
    assert forwarder.records[-1].state is ForwardState.OVERLOADED


async def test_get_command_result_passes_through_without_resubmit(
    tmp_path: Path,
) -> None:
    commander = FakeCommanderService()
    forwarder, _, server = await _pair(tmp_path, commander=commander)
    try:
        forwarder.capture(
            event_id="evt-q",
            gateway_id="gw1",
            device_id="D01",
            point_id="cmd_uint",
            value=3,
            revision="rev",
        )
        record = await _terminal(forwarder, "evt-q")
        assert record.command_id
        calls_before = len(commander.calls)
        result = await forwarder.get_command_result(record.command_id)
        assert result.command_id == record.command_id
        assert len(commander.calls) == calls_before  # 查询不重新执行
    finally:
        await forwarder.stop()
        await server.stop(grace=0)
