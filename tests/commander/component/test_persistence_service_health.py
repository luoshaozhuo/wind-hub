"""服务级 write-behind 健康面：worker 异常退出 DEGRADED、排空超时如实报告。

不依赖 Docker/真实 NATS：注入内存 AuditTransport，配置树复制生产
configs/instances + templates（仅解析，不发起任何连接）。故障通过
EventRepository 子类注入，不在运行时替换实例方法。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from pathlib import Path
from shutil import copytree
from typing import Any

import pytest

from commander.application.service_lifecycle import CommanderService, ServiceState
from commander.application.settings import ServiceSettings
from commander.domain.command import CommandSpec
from commander.infrastructure.persistence.event_repository import EventRepository, RepositoryError
from core.application.protocol_registry import ProtocolRegistry

pytestmark = pytest.mark.component

_REPO_CONFIGS = Path(__file__).resolve().parents[3] / "configs"


class _MemoryTransport:
    def __init__(self) -> None:
        self.published: list[Mapping[str, Any]] = []
        self.closed = False

    async def publish(self, event: Mapping[str, Any]) -> None:
        self.published.append(event)

    async def close(self) -> None:
        self.closed = True


class _AppendFailsRepository(EventRepository):
    """append_events 永久失败：模拟长期 SQLite 不可用。"""

    def append_events(self, events) -> None:
        raise RepositoryError("simulated permanent SQLite outage")


class _AppendKillsWorkerRepository(EventRepository):
    """append_events 抛出 BaseException 级 CancelledError：杀死 worker 任务。

    PersistenceWorker._run 只捕获 Exception；任务死亡必须被看门狗看见。
    """

    def append_events(self, events) -> None:
        raise asyncio.CancelledError("simulated unexpected worker exit")


def _service(
    tmp_path: Path, *, journal_cls: type[EventRepository] = EventRepository, grace: float = 1.0
) -> CommanderService:
    root = tmp_path / "config"
    copytree(_REPO_CONFIGS / "instances", root / "instances")
    copytree(_REPO_CONFIGS / "templates", root / "templates")
    db_path = tmp_path / "state" / "commander.db"

    def load_settings() -> ServiceSettings:
        return ServiceSettings(
            file_version="1.0.0",
            devices_path=root / "instances" / "devices.yaml",
            templates_root=root / "templates",
            queue_timeout=1.0,
            connect_timeout=3.0,
            write_timeout=3.0,
            idle_timeout=30.0,
            watchdog_interval=0.05,
            global_capacity=10,
            max_queued_batches_per_device=10,
            max_queued_batches_global=100,
            max_batch_points=10,
            sqlite_path=db_path,
            max_storage_bytes=64 * 1024**3,
            reserved_storage_bytes=64 * 1024**2,
            admission_stop_ratio=0.9,
            retention_seconds=1800,
            cleanup_interval_seconds=3600.0,
            audit_stream="S",
            audit_subject="sub",
            nats_servers=("nats://127.0.0.1:1",),
            audit_retry_base=0.01,
            audit_retry_max=0.05,
            shutdown_grace_seconds=grace,
            persistence_interval_seconds=0.02,
            persistence_batch_size=500,
            persistence_max_events=100_000,
            # 故障注入用例：持久化门禁快速超时，避免等待生产默认 10s。
            accept_commit_timeout=0.3,
        )

    return CommanderService(
        protocol_registry=ProtocolRegistry(),
        audit_transport=_MemoryTransport(),
        settings_loader=load_settings,
        journal_factory=lambda settings: journal_cls(settings.sqlite_path),
    )


async def _shutdown_leaked_resources(service: CommanderService) -> None:
    # FAILED 早退路径按设计把进程交还给外部监督者重启；测试内显式回收线程/连接。
    writer = service._writer  # noqa: SLF001
    journal = service._journal  # noqa: SLF001
    if writer is not None:
        if journal is not None:
            with contextlib.suppress(Exception):
                await asyncio.get_running_loop().run_in_executor(writer, journal.close)
        await asyncio.to_thread(writer.shutdown, wait=True)


async def _wait_state(
    service: CommanderService, state: ServiceState, *, timeout: float = 2.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if service.health().state is state:
            return
        await asyncio.sleep(0.02)
    assert service.health().state is state


async def test_persistence_worker_death_is_degraded(tmp_path: Path) -> None:
    service = _service(tmp_path, journal_cls=_AppendKillsWorkerRepository)
    await service.start()
    try:
        assert service.health().state is ServiceState.READY
        assert service.health().ready is True
        # 受理一条命令：T1 入内存队列即触发后台 append，worker 任务死亡；
        # RR-01 门禁等不到 COMMIT，在 accept_commit_timeout 处 fail-closed。
        with pytest.raises(TimeoutError):
            await service.control.accept(
                [CommandSpec("WT01", "setpoint_power", 1.0, "ref-not-checked")],
                caller_id="op",
                request_id="req-health-worker-death",
            )
        await _wait_state(service, ServiceState.DEGRADED)
        health = service.health()
        assert health.ready is False
        # worker 已死：停机不得伪装干净，明确报告持久化队列不安全。
        stopped = await service.stop()
        assert stopped == frozenset({"__persistence_queue__"})
        assert service.health().state is ServiceState.FAILED
    finally:
        await _shutdown_leaked_resources(service)


async def test_stop_reports_persistence_queue_when_drain_times_out(tmp_path: Path) -> None:
    service = _service(tmp_path, journal_cls=_AppendFailsRepository, grace=1.0)
    await service.start()
    try:
        # 直接走控制面受理：T1/保守终态事件入内存队列，后台落盘持续失败、
        # 看门狗降级；RR-01 门禁在 accept_commit_timeout 处抛 TimeoutError。
        with pytest.raises(TimeoutError):
            await service.control.accept(
                [CommandSpec("WT01", "setpoint_power", 1.0, "ref-not-checked")],
                caller_id="op",
                request_id="req-health-drain-timeout",
            )
        await _wait_state(service, ServiceState.DEGRADED)
        # 排空在 grace 预算内不可能成功：stop 必须明确报告，绝不伪装干净停机。
        stopped = await service.stop()
        assert stopped == frozenset({"__persistence_queue__"})
        assert service.health().state is ServiceState.FAILED
    finally:
        await _shutdown_leaked_resources(service)
