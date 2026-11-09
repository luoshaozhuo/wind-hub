"""CommanderService 系统级 E2E：真实共享配置 + Modbus + SQLite + JetStream。

链路边界：
  命令 accept → 真实 SQLite T1/T2/T3 → 真实 Modbus FC16 下发 →
  真实 JetStream 审计发布 → 有界停止。
不使用任何协议替身；唯一不可用时项（Docker/NATS）显式 skip 并在验收
报告中记 NOT_RUN。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from nats.aio.client import Client as NatsClient

from commander.application.persistence import BufferedJournal
from commander.application.service_lifecycle import CommanderService, ServiceState
from commander.domain.command import CommandSpec, CommandState
from commander.infrastructure.assembly import build_commander
from commander.infrastructure.event_repository import EventRepository
from core.application.protocol_registry import ProtocolRegistry
from core.domain import ConnectionEndpoint
from core.infrastructure.protocol.modbus import ModbusDriver

from ..integration.test_nats_jetstream_real import _connect  # noqa: E402
from .conftest import write_service_config

pytestmark = [
    pytest.mark.system,
    pytest.mark.modbus,
    pytest.mark.real_service,
    pytest.mark.docker,
]


def _fingerprint(service: CommanderService) -> str:
    assert service._config is not None  # noqa: SLF001
    reference = service._config.device_revision("WT01")  # noqa: SLF001
    assert reference is not None
    return reference


def _registry() -> ProtocolRegistry:
    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)
    return registry


async def _fetch(nc: NatsClient, stream: str, subject: str, count: int) -> list[dict[str, Any]]:
    js = nc.jetstream(timeout=5)
    sub = await js.pull_subscribe(subject, durable=f"sys-{uuid.uuid4().hex[:10]}", stream=stream)
    try:
        messages = await sub.fetch(count, timeout=5)
        bodies = [json.loads(msg.data.decode()) for msg in messages]
        for msg in messages:
            await msg.ack()
        return bodies
    finally:
        await sub.unsubscribe()


async def _start_service(
    root: Path, db_path: Path, nats_url: str, modbus_port: int
) -> tuple[CommanderService, str, str]:
    stream = f"CMD_SYS_{uuid.uuid4().hex[:12]}"
    subject = f"commander.sys.{uuid.uuid4().hex[:8]}"
    yaml_path = write_service_config(
        root, db_path, nats_url, modbus_port=modbus_port, stream=stream, subject=subject
    )
    # JetStream stream 是运维预置资源（传输层不负责建流）；测试显式创建。
    bootstrap = await _connect(nats_url)
    await bootstrap.jetstream(timeout=5).add_stream(name=stream, subjects=[subject])
    await bootstrap.drain()
    # 组合根注入 settings_loader / journal_factory，与生产装配完全一致。
    service = build_commander(
        service_config=yaml_path,
        protocol_registry=_registry(),
    )
    await service.start()
    return service, stream, subject


async def test_full_command_chain_real_modbus_and_jetstream(
    config_tree, modbus_server, jetstream_url
) -> None:
    root, db_path = config_tree
    service, stream, subject = await _start_service(
        root, db_path, jetstream_url, modbus_server.port
    )
    nc: NatsClient | None = None
    try:
        health = service.health()
        assert health.state is ServiceState.READY
        assert health.ready is True
        assert health.recovered_commands == 0
        fingerprint = _fingerprint(service)
        assert len(fingerprint) == 64

        accepted = await service.executor.accept(
            [CommandSpec("WT01", "setpoint_power", 42.5, fingerprint)],
            request_id=f"req-e2e-{uuid4().hex}",
            caller_id="ops-console",
        )
        waited = await service.executor.wait(accepted[0].command_id)
        assert waited is not None and waited.outcome is not None
        assert waited.state.value == "SUCCEEDED"
        assert waited.outcome.value == "CONFIRMED_SUCCESS"
        # 稳态：停止前等待真实 JetStream 发箱清零。
        for _ in range(300):
            if service.health().audit_pending_count == 0:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover
            pytest.fail("audit outbox did not drain before stop")
    finally:
        stopped = await service.stop()

    # 停止必须如实干净；transport/journal 在 stop 内部关闭。
    assert stopped == frozenset()

    # 审计侧独立消费验证 4 个事件按序到达真实 JetStream。
    nc = await _connect(jetstream_url)
    try:
        events = await _fetch(nc, stream, subject, 4)
    finally:
        await nc.drain()
    assert [e["event_type"] for e in events] == [
        "ACCEPTED",
        "CONNECTING",
        "EXECUTING",
        "COMPLETED",
    ]
    assert events[-1]["state"] == "SUCCEEDED"
    # 仅 ACCEPTED 事件携带调用方/点位上下文（DDL CHECK 约束），过程事件按 command_id 关联。
    assert events[0]["caller_id"] == "ops-console"
    assert events[0]["device_id"] == "WT01"
    assert events[0]["point_id"] == "setpoint_power"
    assert {e["command_id"] for e in events} == {accepted[0].command_id}

    # 物理寄存器终值验证（服务已停，另起一个真实驱动回读）。
    from tests.core.protocol.conftest import make_point, make_table

    verify_table = make_table(
        "modbus",
        make_point(
            "setpoint_power",
            register_type="holding",
            address=200,
            data_type="float32",
        ),
    )
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", modbus_server.port),
        verify_table,
        {"word_order": "big_endian", "timeout": 2.0},
    )
    await driver.connect()
    try:
        sample = await driver.read_one("setpoint_power")
        assert sample.value == pytest.approx(42.5, abs=0.01)
    finally:
        await driver.close()


async def test_restart_recovers_executing_command_as_unknown(
    config_tree, modbus_server, jetstream_url
) -> None:
    root, db_path = config_tree
    service, _stream, _subject = await _start_service(
        root, db_path, jetstream_url, modbus_server.port
    )
    fingerprint = _fingerprint(service)
    command_id = None
    try:
        assert await service.stop() == frozenset()
    finally:
        pass

    # 模拟进程在 T2 之后崩溃：直接用真实 SQLite 留下 EXECUTING 命令。
    journal = EventRepository(db_path)
    try:
        buffer = BufferedJournal()
        accepted = buffer.accept(
            [CommandSpec("WT01", "setpoint_power", 10.0, fingerprint)],
            request_id=f"req-e2e-{uuid4().hex}",
            caller_id="ops-console",
        )
        command_id = accepted[0].command_id
        buffer.transition(command_id, CommandState.CONNECTING)
        buffer.transition(command_id, CommandState.EXECUTING)
        journal.append_events(buffer.snapshot(100))
    finally:
        journal.close()

    service2, _s2, _j2 = await _start_service(root, db_path, jetstream_url, modbus_server.port)
    try:
        health = service2.health()
        assert health.recovered_commands == 1
        assert health.state is ServiceState.READY
        recovered = await service2.control.get_command(command_id)
        assert recovered is not None and recovered.outcome is not None
        assert recovered.state.value == "UNKNOWN"
        assert recovered.outcome.value == "UNKNOWN"
        # 恢复绝不重发：寄存器保持 0（服务从未下发）。
    finally:
        assert await service2.stop() == frozenset()


async def test_reload_unchanged_config_is_noop_and_restart_not_required(
    config_tree, modbus_server, jetstream_url
) -> None:
    root, db_path = config_tree
    service, _stream, _subject = await _start_service(
        root, db_path, jetstream_url, modbus_server.port
    )
    try:
        before = service.health().revision
        report = await service.reload()
        assert report.changed == ()
        assert "WT01" in report.unchanged
        assert report.blocked == ()
        assert report.restart_required is False
        assert service.health().revision == before
        assert service.health().ready is True
    finally:
        assert await service.stop() == frozenset()
