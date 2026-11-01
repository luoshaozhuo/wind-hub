"""真实 NATS JetStream 集成：PubAck 校验、Nats-Msg-Id 去重、发箱投递。

依赖本机 nats://127.0.0.1:4222（docker r5/soak 栈）。不可达时整个模块
明确 skip（验收报告记 NOT_RUN），绝不以内存替身冒充。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import nats
import pytest
from nats.aio.client import Client as NatsClient

from commander.application.audit import AuditDelivery
from commander.domain.command import (
    CommandOutcome,
    CommandSpec,
    CommandState,
)
from commander.infrastructure.audit_publisher import AuditPublisher, JetStreamClient
from commander.infrastructure.event_repository import EventRepository
from commander.infrastructure.nats_audit import NatsAuditTransport
from tests.support.process import free_port

pytestmark = [pytest.mark.integration, pytest.mark.real_service, pytest.mark.docker]


@pytest.fixture(scope="module")
def nats_server() -> Iterator[str]:
    """自管独立 JetStream 容器（随机端口），绝不复用/干扰 soak 的 4222 容器。"""
    try:
        subprocess.run(["docker", "version"], capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"SKIPPED: docker unavailable for JetStream test: {exc}")
    port = free_port()
    name = f"cmd-accept-js-{uuid.uuid4().hex[:10]}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{port}:4222",
                "nats:latest",
                "-js",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except subprocess.SubprocessError as exc:
        pytest.skip(f"SKIPPED: cannot start JetStream container: {exc}")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            import socket

            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.skip("SKIPPED: JetStream container did not open client port")
    # JetStream 元数据就绪再放行。
    time.sleep(1.0)
    try:
        yield f"nats://127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)


async def _connect(url: str) -> NatsClient:
    return await asyncio.wait_for(nats.connect(url, allow_reconnect=False), timeout=5.0)


@pytest.fixture
async def jetstream(
    nats_server: str,
) -> AsyncIterator[tuple[NatsClient, str, str, str]]:
    nc = await _connect(nats_server)
    stream = f"CMD_ACCEPT_{uuid.uuid4().hex[:12]}"
    subject = f"commander.accept.{uuid.uuid4().hex[:8]}"
    js = nc.jetstream(timeout=5)
    await js.add_stream(name=stream, subjects=[subject])
    try:
        yield nc, stream, subject, nats_server
    finally:
        with contextlib.suppress(Exception):
            await js.delete_stream(stream)
        await nc.drain()


async def _fetch_all(js: Any, stream: str, subject: str, count: int) -> list[bytes]:
    durable = f"d-{uuid.uuid4().hex[:12]}"
    sub = await js.pull_subscribe(subject, durable=durable, stream=stream)
    try:
        messages = await sub.fetch(count, timeout=5)
        payloads = [msg.data for msg in messages]
        for msg in messages:
            await msg.ack()
        return payloads
    finally:
        await sub.unsubscribe()


def _event(event_id: str, event_type: str = "ACCEPTED") -> dict[str, str]:
    return {"event_id": event_id, "event_type": event_type, "command_id": "c1"}


async def test_publisher_requires_ack_from_expected_stream(
    jetstream: tuple[NatsClient, str, str, str],
) -> None:
    nc, stream, subject, _url = jetstream
    # PubAck 必须来自声明的目标流；错误流名称必须 fail-closed。
    wrong = AuditPublisher(
        cast(JetStreamClient, nc.jetstream(timeout=5)), subject, "DOES_NOT_EXIST"
    )
    with pytest.raises(Exception):  # noqa: B017, BLE001
        await wrong.publish(_event("evt-x"))


async def test_nats_msg_id_deduplicates_republished_event(
    jetstream: tuple[NatsClient, str, str, str],
) -> None:
    nc, stream, subject, _url = jetstream
    js = nc.jetstream(timeout=5)
    publisher = AuditPublisher(cast(JetStreamClient, js), subject, stream)
    event = _event("evt-dup-1")
    await publisher.publish(event)
    await publisher.publish(event)  # 同 event_id / Nats-Msg-Id 重发
    payloads = await _fetch_all(js, stream, subject, 1)
    # JetStream 按 Nats-Msg-Id 去重：流内只有一条消息。
    assert len(payloads) == 1
    body = json.loads(payloads[0].decode())
    assert body["event_id"] == "evt-dup-1"
    assert "publish_state" not in body and "published_at" not in body and "attempts" not in body


async def test_publisher_rejects_events_without_identity_or_valid_type(
    jetstream: tuple[NatsClient, str, str, str],
) -> None:
    nc, stream, subject, _url = jetstream
    publisher = AuditPublisher(cast(JetStreamClient, nc.jetstream(timeout=5)), subject, stream)
    with pytest.raises(ValueError):
        await publisher.publish({"event_type": "ACCEPTED"})
    with pytest.raises(ValueError):
        await publisher.publish({"event_id": "x", "event_type": "WEIRD"})


async def test_audit_delivery_over_real_jetstream_drains_outbox_in_order(
    jetstream: tuple[NatsClient, str, str, str], tmp_path
) -> None:
    nc, stream, subject, url = jetstream
    js = nc.jetstream(timeout=5)
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="it-sqlite")
    repo = writer.submit(lambda: EventRepository(tmp_path / "cmd.db")).result()
    transport = NatsAuditTransport([url], stream=stream, subject=subject)
    delivery = AuditDelivery(repo, transport, writer, retry_base=0.1, retry_max=1.0)

    def seed() -> None:
        command = repo.accept([CommandSpec("WT01", "p", 3.5, "ref-v1")], caller_id="ops")[0]
        repo.transition(command.command_id, CommandState.CONNECTING)
        repo.transition(command.command_id, CommandState.EXECUTING)
        repo.transition(
            command.command_id,
            CommandState.SUCCEEDED,
            outcome=CommandOutcome.CONFIRMED_SUCCESS,
        )

    writer.submit(seed).result()
    delivery.start()
    try:
        for _ in range(300):
            if not writer.submit(repo.pending_events, 100).result():
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover
            pytest.fail("real JetStream outbox did not drain")
        assert await delivery.stop(timeout=5.0)
    finally:
        if delivery.operational:
            await delivery.stop(timeout=5.0)
        writer.submit(repo.close).result()
        writer.shutdown(wait=True)
        await transport.close()
    payloads = await _fetch_all(js, stream, subject, 4)
    types = [json.loads(p)["event_type"] for p in payloads]
    assert types == ["ACCEPTED", "CONNECTING", "EXECUTING", "COMPLETED"]
    states = [json.loads(p)["state"] for p in payloads]
    assert states == ["ACCEPTED", "CONNECTING", "EXECUTING", "SUCCEEDED"]


async def test_mixed_result_serial_batch_audit_events_complete_ordered_unique(
    jetstream: tuple[NatsClient, str, str, str], tmp_path
) -> None:
    """N1 混合终态批次（成功/拒绝/UNKNOWN/NOT_SENT）的审计事件齐全、有序、不重复。"""
    from commander.application.control import CommandControl
    from commander.application.queued_executor import QueuedExecutor
    from core.application.protocol_contract import (
        ProtocolCapability,
        ProtocolWrite,
        ProtocolWriteResult,
        WriteOutcome,
    )

    from ..component.conftest import make_spec

    nc, stream, subject, url = jetstream
    js = nc.jetstream(timeout=5)
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="it-sqlite")
    repo = writer.submit(lambda: EventRepository(tmp_path / "mixed.db")).result()
    control = CommandControl(repo, writer)
    transport = NatsAuditTransport([url], stream=stream, subject=subject)
    delivery = AuditDelivery(repo, transport, writer, retry_base=0.1, retry_max=1.0)

    class _ScriptedSerialSession:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def capabilities(self) -> frozenset[ProtocolCapability]:
            return frozenset({ProtocolCapability.READ, ProtocolCapability.WRITE})

        def is_open(self) -> bool:
            return True

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
            self.calls.append(write.point)
            if write.point == "p1":
                return ProtocolWriteResult(write.point, WriteOutcome.CONFIRMED_FAILURE)
            if write.point == "p2":
                raise ConnectionError("link reset mid-batch")
            return ProtocolWriteResult(write.point, WriteOutcome.CONFIRMED_SUCCESS)

        async def write_many(self, writes: Any) -> tuple[ProtocolWriteResult, ...]:
            raise AssertionError("session does not advertise WRITE_MANY")

    session = _ScriptedSerialSession()
    executor = QueuedExecutor(
        control,
        lambda _device_id: session,
        lambda _spec: True,
        lambda spec: ProtocolWrite(point=spec.point_id, value=spec.value),
        max_batch_points=10,
        write_timeout=3.0,
    )

    delivery.start()
    accepted = await executor.accept(
        [make_spec(f"p{i}", device="WT01") for i in range(4)], caller_id="ops"
    )
    try:
        await asyncio.gather(
            *(executor.wait(c.command_id) for c in accepted),
            return_exceptions=True,
        )
        assert session.calls == ["p0", "p1", "p2"]
    finally:
        await executor.close()
    expected_states = ["SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"]
    expected_outcomes = [
        "CONFIRMED_SUCCESS",
        "CONFIRMED_FAILURE",
        "UNKNOWN",
        "NOT_SENT",
    ]
    # 审计投影与仓储查询一致（须在 writer 关闭前经控制面查询）。
    for index, accepted_command in enumerate(accepted):
        record = await control.get_command(accepted_command.command_id)
        assert record is not None
        assert record.state.value == expected_states[index]
        assert record.outcome is not None and record.outcome.value == expected_outcomes[index]
    try:
        for _ in range(300):
            if not writer.submit(repo.pending_events, 100).result():
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover
            pytest.fail("mixed-batch outbox did not drain")
        assert await delivery.stop(timeout=5.0)
    finally:
        if delivery.operational:
            await delivery.stop(timeout=5.0)
        writer.submit(repo.close).result()
        writer.shutdown(wait=True)
        await transport.close()

    payloads = await _fetch_all(js, stream, subject, 12)
    events = [json.loads(payload) for payload in payloads]
    assert len({event["event_id"] for event in events}) == 12
    # 全局顺序：4 ACCEPTED → 4 EXECUTING（单 T2 事务）→ 4 COMPLETED（单 T3 事务）。
    assert [event["event_type"] for event in events] == [
        "ACCEPTED",
        "ACCEPTED",
        "ACCEPTED",
        "ACCEPTED",
        "EXECUTING",
        "EXECUTING",
        "EXECUTING",
        "EXECUTING",
        "COMPLETED",
        "COMPLETED",
        "COMPLETED",
        "COMPLETED",
    ]
    point_by_command = {c.command_id: c.spec.point_id for c in accepted}
    completed = events[8:]
    by_point = {point_by_command[event["command_id"]]: event for event in completed}
    assert [by_point[f"p{i}"]["state"] for i in range(4)] == expected_states
    assert [by_point[f"p{i}"]["outcome"] for i in range(4)] == expected_outcomes
