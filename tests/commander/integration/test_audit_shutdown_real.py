"""B3 复测：真实 NATS JetStream 停机末轮排空 / 持续中断有界 / 重启补发。

关键路径全部走真实容器（docker stop/start 注入中断），不用替身。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from commander.application.audit import AuditDelivery
from commander.domain.command import CommandOutcome, CommandSpec, CommandState
from commander.infrastructure.adapters.outbound.nats_audit import NatsAuditTransport
from commander.infrastructure.persistence.event_repository import EventRepository
from tests.support.process import free_port

from ..seed_events import seed_accept, seed_transition
from .test_nats_jetstream_real import _connect, _fetch_all

pytestmark = [pytest.mark.integration, pytest.mark.real_service, pytest.mark.docker]


@pytest.fixture(scope="module")
def nats_container() -> Iterator[tuple[str, str]]:
    """返回 (url, container_name)，供中断测试 docker stop/start。"""
    try:
        subprocess.run(["docker", "version"], capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"SKIPPED: docker unavailable for JetStream test: {exc}")
    port = free_port()
    name = f"cmd-b3-js-{uuid.uuid4().hex[:10]}"
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
        with (
            contextlib.suppress(OSError),
            socket.create_connection(("127.0.0.1", port), timeout=0.5),
        ):
            break
        time.sleep(0.2)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.skip("SKIPPED: JetStream container did not open client port")
    time.sleep(1.0)
    try:
        yield f"nats://127.0.0.1:{port}", name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)


async def _wait_nats_ready(url: str) -> None:
    """docker start 后 TCP 先通、协议未就绪；以真实 NATS 握手为准。"""
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            nc = await _connect(url)
            await nc.drain()
            return
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.3)
    pytest.fail("NATS did not become ready after container restart")


def _wait_port(port: int, *, open_expected: bool) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                is_open = True
        except OSError:
            is_open = False
        if is_open == open_expected:
            return
        time.sleep(0.2)
    pytest.fail("NATS container port did not reach expected state in time")


async def _stream(url: str) -> tuple[Any, str, str]:
    nc = await _connect(url)
    stream = f"CMD_B3_{uuid.uuid4().hex[:12]}"
    subject = f"commander.b3.{uuid.uuid4().hex[:8]}"
    await nc.jetstream(timeout=5).add_stream(name=stream, subjects=[subject])
    return nc, stream, subject


def _full_chain(repo: EventRepository, value: float) -> None:
    command = seed_accept(repo, [CommandSpec("WT01", "p", value, "ref-v1")], caller_id="ops")[0]
    seed_transition(repo, command.command_id, CommandState.CONNECTING)
    seed_transition(repo, command.command_id, CommandState.EXECUTING)
    seed_transition(
        repo,
        command.command_id,
        CommandState.SUCCEEDED,
        outcome=CommandOutcome.CONFIRMED_SUCCESS,
    )


async def test_stop_drains_full_pending_outbox(nats_container, tmp_path) -> None:
    """停止时 outbox 积压 12 条：stop 必须末轮排空后才返回 True。"""
    url, _name = nats_container
    nc, stream, subject = await _stream(url)
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="b3-sqlite")
    repo = writer.submit(lambda: EventRepository(tmp_path / "b3.db")).result()
    transport = NatsAuditTransport([url], stream=stream, subject=subject)
    delivery = AuditDelivery(repo, transport, writer, retry_base=0.1, retry_max=1.0)
    for i in range(3):
        writer.submit(_full_chain, repo, float(i + 1)).result()
    delivery.start()
    try:
        # 不等后台自然排空；stop 自身负责末轮排空。
        assert await delivery.stop(timeout=15.0) is True
        assert not writer.submit(repo.pending_events, 100).result()
    finally:
        if delivery.operational:
            await delivery.stop(timeout=5.0)
        writer.submit(repo.close).result()
        writer.shutdown(wait=True)
        await transport.close()
    try:
        payloads = await _fetch_all(nc.jetstream(timeout=5), stream, subject, 12)
        assert len(payloads) == 12
    finally:
        await nc.drain()


async def test_persistent_outage_bounded_stop_then_restart_republishes(
    nats_container, tmp_path
) -> None:
    """NATS 持续不可用：stop 有界返回 False、事件留 PENDING；重启实例后补发。"""
    url, name = nats_container
    nc, stream, subject = await _stream(url)
    # 建流后立即释放：本用例随后会停掉整个容器，不让此连接跨越中断窗口。
    await nc.drain()
    db_path = tmp_path / "b3_restart.db"
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="b3-sqlite")
    repo = writer.submit(lambda: EventRepository(db_path)).result()
    transport = NatsAuditTransport([url], stream=stream, subject=subject)
    delivery = AuditDelivery(repo, transport, writer, retry_base=0.1, retry_max=1.0)
    writer.submit(_full_chain, repo, 1.0).result()
    delivery.start()
    for _ in range(300):
        if not writer.submit(repo.pending_events, 100).result():
            break
        await asyncio.sleep(0.02)
    else:  # pragma: no cover
        pytest.fail("baseline outbox did not drain")

    # 注入持续中断：停容器后写入新一批 PENDING。
    port = url.rsplit(":", 1)[1]
    subprocess.run(["docker", "stop", name], capture_output=True, check=True, timeout=20)
    _wait_port(int(port), open_expected=False)
    try:
        writer.submit(_full_chain, repo, 2.0).result()
        started = time.monotonic()
        assert await delivery.stop(timeout=8.0) is False
        elapsed = time.monotonic() - started
        assert elapsed < 15.0, "stop must remain bounded under persistent outage"
        # shield 保护的在途发布不可取消：stop 到点返回 False 后，等该原生 I/O
        # 失败返回、worker 自查停机 deadline 后自行终止（不被遗忘）。
        for _ in range(750):
            if not delivery.operational:
                break
            await asyncio.sleep(0.02)
        assert delivery.operational is False
        pending = writer.submit(repo.pending_events, 100).result()
        assert len(pending) == 4
        # 停机窗口内首个失败即如实终止 worker；至少队首留下失败簿记，
        # 其余未尝试事件保持 PENDING（attempts=0），重启后全部补发。
        assert any(row["attempts"] >= 1 for row in pending)
        writer.submit(repo.close).result()
        writer.shutdown(wait=True)

        # 恢复 NATS；服务器就绪后再 drain 旧连接，避免后台重连任务残留。
        subprocess.run(["docker", "start", name], capture_output=True, check=True, timeout=20)
        await _wait_nats_ready(url)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(transport.close(), timeout=5.0)
        # 新进程语义：新 writer 线程归属新连接。
        writer2 = ThreadPoolExecutor(max_workers=1, thread_name_prefix="b3-sqlite-r")
        repo2 = writer2.submit(lambda: EventRepository(db_path)).result()
        transport2 = NatsAuditTransport([url], stream=stream, subject=subject)
        delivery2 = AuditDelivery(repo2, transport2, writer2, retry_base=0.1, retry_max=1.0)
        delivery2.start()
        try:
            for _ in range(500):
                if not writer2.submit(repo2.pending_events, 100).result():
                    break
                await asyncio.sleep(0.02)
            else:  # pragma: no cover
                pytest.fail("restart redelivery did not drain")
            assert await delivery2.stop(timeout=10.0) is True
        finally:
            if delivery2.operational:
                await delivery2.stop(timeout=5.0)
            writer2.submit(repo2.close).result()
            writer2.shutdown(wait=True)
            await transport2.close()
    finally:
        with contextlib.suppress(subprocess.SubprocessError):
            subprocess.run(["docker", "start", name], capture_output=True, timeout=20)

    # 两条命令链共 8 事件；Nats-Msg-Id 去重下重发不产生重复审计记录。
    # 用全新连接消费：bootstrap 客户端经历过整个中断窗口。
    consumer_nc = await _connect(url)
    try:
        payloads = await _fetch_all(consumer_nc.jetstream(timeout=5), stream, subject, 8)
    finally:
        await consumer_nc.drain()
    assert len(payloads) == 8
    event_ids = [json.loads(p)["event_id"] for p in payloads]
    assert len(set(event_ids)) == 8
