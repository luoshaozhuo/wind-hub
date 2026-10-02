"""Server 进程内 Worker Registry。

Registry 只维护控制面已知 Worker 的身份、endpoint 与最近一次探测事实，不负责
进程拉起、任务分配或 RPC 路由选择。当前固定登记一个 Collector 和一个 Commander，
为后续多 Worker/主备扩展提供稳定状态模型。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel

from wind_hub_server.application.worker_model import (
    WorkerCapability,
    WorkerDefinition,
    WorkerRole,
)
from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.port.worker import CommanderPort


class WorkerState(StrEnum):
    """Server 对 Worker 的最近可观测连接状态。"""

    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"


class WorkerRecord(BaseModel):
    """单个 Worker 的控制面状态快照。"""

    worker_id: str
    role: WorkerRole
    endpoint: str
    capabilities: list[WorkerCapability]
    reported_id: str | None = None
    state: WorkerState = WorkerState.UNKNOWN
    last_probe_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_error: str | None = None
    runtime_running: bool | None = None
    active_revision: str | None = None
    active_config_hash: str | None = None
    boot_id: str | None = None


class WorkerRegistryUseCase:
    """维护当前 Server 已知 Worker 的最近状态。"""

    def __init__(
        self,
        collectors: CollectorDirectory,
        commander: CommanderPort,
        *,
        definitions: list[WorkerDefinition],
    ) -> None:
        self._collectors = collectors
        self._commander = commander
        self._lock = asyncio.Lock()
        self._records = {
            definition.worker_id: WorkerRecord(
                worker_id=definition.worker_id,
                role=definition.role,
                endpoint=definition.endpoint,
                capabilities=list(definition.capabilities),
            )
            for definition in definitions
        }
        self._collector_worker_id = self._worker_id_for_role(WorkerRole.COLLECTOR)
        self._commander_worker_id = self._worker_id_for_role(WorkerRole.COMMANDER)
        self._collectors.get(self._collector_worker_id)

    async def refresh(self) -> list[WorkerRecord]:
        """并发探测全部已登记 Worker，并原子更新最近状态。"""
        collector_result, commander_result = await asyncio.gather(
            self._probe_collector(),
            self._probe_commander(),
        )
        async with self._lock:
            self._records[self._collector_worker_id] = collector_result
            self._records[self._commander_worker_id] = commander_result
            return self._snapshot_unlocked()

    async def list_workers(self) -> list[WorkerRecord]:
        """返回按角色稳定排序的 Worker 状态快照。"""
        async with self._lock:
            return self._snapshot_unlocked()

    async def get_worker(self, worker_id: str) -> WorkerRecord:
        """返回指定 Worker 状态。"""
        async with self._lock:
            try:
                return self._records[worker_id].model_copy(deep=True)
            except KeyError as exc:
                raise KeyError(worker_id) from exc

    async def _probe_collector(self) -> WorkerRecord:
        """读取 Collector 身份/配置状态；RPC 失败只更新 Registry 状态。"""
        now = datetime.now(UTC)
        previous = self._records[self._collector_worker_id]
        try:
            collector = self._collectors.get(self._collector_worker_id)
            status = await collector.config_status()
        except Exception as exc:
            return self._offline(previous, now, exc)
        return WorkerRecord(
            worker_id=previous.worker_id,
            role=previous.role,
            endpoint=previous.endpoint,
            capabilities=list(previous.capabilities),
            reported_id=_optional_text(status.get("collector_id")),
            state=WorkerState.ONLINE,
            last_probe_at=now,
            last_seen_at=now,
            last_error=None,
            runtime_running=bool(status.get("runtime_running")),
            active_revision=_optional_text(status.get("active_revision")),
            active_config_hash=_optional_text(status.get("active_config_hash")),
            boot_id=_optional_text(status.get("boot_id")),
        )

    async def _probe_commander(self) -> WorkerRecord:
        """读取 Commander 运行/配置状态；RPC 失败只更新 Registry 状态。"""
        now = datetime.now(UTC)
        previous = self._records[self._commander_worker_id]
        try:
            status = await self._commander.status()
        except Exception as exc:
            return self._offline(previous, now, exc)
        return WorkerRecord(
            worker_id=previous.worker_id,
            role=previous.role,
            endpoint=previous.endpoint,
            capabilities=list(previous.capabilities),
            reported_id=previous.reported_id,
            state=WorkerState.ONLINE,
            last_probe_at=now,
            last_seen_at=now,
            last_error=None,
            runtime_running=bool(status.get("running")),
            active_revision=_optional_text(status.get("active_revision")),
            active_config_hash=_optional_text(status.get("active_config_hash")),
            boot_id=None,
        )

    @staticmethod
    def _offline(
        previous: WorkerRecord,
        now: datetime,
        exc: Exception,
    ) -> WorkerRecord:
        """保留最近成功事实，仅把当前连接状态标记为 OFFLINE。"""
        return previous.model_copy(
            update={
                "state": WorkerState.OFFLINE,
                "last_probe_at": now,
                "last_error": str(exc) or type(exc).__name__,
                "runtime_running": None,
            },
            deep=True,
        )

    def _snapshot_unlocked(self) -> list[WorkerRecord]:
        """在锁内复制 Registry 快照。"""
        return [
            self._records[worker_id].model_copy(deep=True)
            for worker_id in sorted(self._records)
        ]

    def _worker_id_for_role(self, role: WorkerRole) -> str:
        """返回指定角色唯一 Worker ID。"""
        matches = [
            worker_id
            for worker_id, record in self._records.items()
            if record.role is role
        ]
        if len(matches) != 1:
            raise ValueError(
                f"expected exactly one {role.value} worker, got {len(matches)}"
            )
        return matches[0]


def _optional_text(value: object) -> str | None:
    """把空值/空字符串统一收敛为 None。"""
    if value is None:
        return None
    text = str(value)
    return text or None
