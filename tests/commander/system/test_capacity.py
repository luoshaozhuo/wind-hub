"""§14.4 表 14-2 容量与审计系统测试（UR-13/UR-14，UC-02-S05/S06）。

* 持久化容量准入关闭时，新控制必须被 RESOURCE_EXHAUSTED 拒绝，恢复后放行；
* 真实 JetStream 链路（自管容器，无 Docker 时 NOT_RUN）：仅已落盘事件
  发布、PubAck 后发箱清空、同一 event_id 重投被 JetStream 去重。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import cast

import grpc
import nats
import pytest

from commander.infrastructure.audit_publisher import AuditPublisher
from contracts import commander_pb2 as pb
from contracts import common_pb2

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.real_service,
]

_TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"}


async def _aborts(call, code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await call
    assert exc_info.value.code() is code, exc_info.value
    return exc_info.value


@pytest.mark.storage
async def test_persistence_and_admission(sys_pki, grpc_stack) -> None:
    """UC-02-S05：故障注入关闭持久化准入 → 拒绝新控制；恢复后重新放行。"""
    _common, commander = grpc_stack.stubs(sys_pki, client="ops")
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "TZ01")
    points = await commander.ListPoints(pb.ListPointsRequest(device_id="TZ01"), timeout=5)
    point = next(p for p in points.points if p.writable)

    executor = grpc_stack.service._executor  # noqa: SLF001
    assert executor is not None
    # 真实准入闸门（watchdog 也会依据 SQLite 使用量驱动同一开关）。
    executor.set_storage_admission(False)
    try:
        await _aborts(
            commander.SubmitCommand(
                pb.SubmitCommandRequest(
                    device_id="TZ01",
                    point_id=point.point_id,
                    value=common_pb2.ScalarValue(sint64_value=1),
                    expected_revision=revision,
                ),
                timeout=5,
            ),
            grpc.StatusCode.RESOURCE_EXHAUSTED,
        )
    finally:
        executor.set_storage_admission(True)
    accepted = await commander.SubmitCommand(
        pb.SubmitCommandRequest(
            device_id="TZ01",
            point_id=point.point_id,
            value=common_pb2.ScalarValue(sint64_value=1),
            expected_revision=revision,
        ),
        timeout=5,
    )
    assert accepted.accepted is True


@pytest.mark.recovery
async def test_audit_retry(sys_pki, grpc_stack_jetstream) -> None:
    """UC-02-S06：真实 JetStream 闭环——落盘后投递、PubAck 确认、同 ID 重投去重。"""
    stack, url = grpc_stack_jetstream
    # 流由部署侧预建（与生产一致，服务不隐式建流）：真实 JetStream 上声明。
    bootstrap = await asyncio.wait_for(nats.connect(url, allow_reconnect=False), timeout=5)
    try:
        await bootstrap.jetstream(timeout=5).add_stream(name=stack.stream, subjects=[stack.subject])
    finally:
        await bootstrap.drain()
    _common, commander = stack.stubs(sys_pki, client="ops")
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "WT01")
    submitted = await commander.SubmitCommand(
        pb.SubmitCommandRequest(
            device_id="WT01",
            point_id="setpoint_power",
            value=common_pb2.ScalarValue(double_value=55.25),
            expected_revision=revision,
        ),
        timeout=5,
    )
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    while datetime.now(UTC) < deadline:
        result = await commander.GetCommandResult(
            pb.GetCommandResultRequest(command_id=submitted.command_id), timeout=5
        )
        if result.state in _TERMINAL:
            break
        await asyncio.sleep(0.05)
    assert result.state == "SUCCEEDED"

    # 等待终态事件先真实落盘（write-behind），再等待 outbox 清空：
    # 只有 PubAck 成功的事件才会被标记已发布。内存终态可见早于落盘，
    # 不能在 T2/T3 事件入箱前就判定排空。
    service = stack.service
    writer = cast(ThreadPoolExecutor, service._writer)  # noqa: SLF001
    journal = service._journal  # noqa: SLF001
    for _ in range(300):
        record = writer.submit(journal.get, submitted.command_id).result()
        if record is not None and record.state.value == "SUCCEEDED":
            break
        await asyncio.sleep(0.02)
    else:  # pragma: no cover
        pytest.fail("terminal event was not persisted before audit drain")
    for _ in range(300):
        if not writer.submit(journal.pending_events, 100).result():
            break
        await asyncio.sleep(0.02)
    else:  # pragma: no cover
        pytest.fail("real JetStream outbox did not drain after PubAck")

    # 独立消费者：四个事件按序到达真实 JetStream 流。
    nc = await asyncio.wait_for(nats.connect(url, allow_reconnect=False), timeout=5)
    try:
        js = nc.jetstream(timeout=5)
        durable = f"d-{uuid.uuid4().hex[:12]}"
        sub = await js.pull_subscribe(stack.subject, durable=durable, stream=stack.stream)
        try:
            messages = await sub.fetch(4, timeout=8)
            payloads = [json.loads(m.data) for m in messages]
            for m in messages:
                await m.ack()
        finally:
            await sub.unsubscribe()
        assert [p["event_type"] for p in payloads] == [
            "ACCEPTED",
            "CONNECTING",
            "EXECUTING",
            "COMPLETED",
        ]
        first = payloads[0]

        # 丢确认后的重投：同一 event_id（Nats-Msg-Id）不得在流内产生第二条。
        publisher = AuditPublisher(js, stack.subject, stack.stream)
        await publisher.publish(
            {
                "event_id": first["event_id"],
                "event_type": "ACCEPTED",
                "command_id": first["command_id"],
            }
        )
        sub2 = await js.pull_subscribe(
            stack.subject, durable=f"d-{uuid.uuid4().hex[:12]}", stream=stack.stream
        )
        try:
            # 新消费者重放全流：重投同 Nats-Msg-Id 未被二次存储，仍只有 4 条。
            stored = await sub2.fetch(4, timeout=5)
            assert len(stored) == 4
            with pytest.raises(asyncio.TimeoutError):
                await sub2.fetch(1, timeout=1.5)
            for m in stored:
                await m.ack()
        finally:
            await sub2.unsubscribe()
    finally:
        await nc.drain()
