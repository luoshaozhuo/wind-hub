"""进程内异步 Operation 状态模型与注册表。

用于 Verify All、Subnet Scan、Config Apply 等跨请求长操作的统一状态基础。
本模块只管理 Operation 生命周期和结果快照，不负责执行具体业务协程，也不
依赖 FastAPI；后续可用持久化/IPC 实现替换而不改变 API 契约。
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class OperationState(str, Enum):
    """Operation 生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationError(BaseModel):
    """Operation 的稳定错误结构。"""

    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class OperationRecord(BaseModel):
    """可跨请求查询的 Operation 快照。"""

    operation_id: str
    kind: str
    state: OperationState = OperationState.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total: int = Field(default=0, ge=0)
    completed: int = Field(default=0, ge=0)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    result: dict[str, object] | None = None
    error: OperationError | None = None


_TERMINAL_STATES = {
    OperationState.SUCCESS,
    OperationState.PARTIAL,
    OperationState.FAILED,
    OperationState.CANCELLED,
}


class OperationManager:
    """线程安全的进程内 Operation 注册表。

    当前实现不持久化，适用于单进程 wind-hub-server。Operation 执行逻辑由
    调用方负责；本类只提供创建、状态推进和查询，避免 API 层持有业务任务。
    """

    def __init__(self) -> None:
        self._items: dict[str, OperationRecord] = {}
        self._lock = threading.RLock()

    def create(self, kind: str, *, total: int = 0) -> OperationRecord:
        """创建 pending Operation 并返回独立快照。"""
        if not kind.strip():
            raise ValueError("operation kind must not be empty")
        if total < 0:
            raise ValueError("operation total must not be negative")
        record = OperationRecord(
            operation_id=str(uuid.uuid4()),
            kind=kind,
            total=total,
        )
        with self._lock:
            self._items[record.operation_id] = record
        return record.model_copy(deep=True)

    def get(self, operation_id: str) -> OperationRecord:
        """查询 Operation；未知 ID 抛 KeyError。"""
        with self._lock:
            record = self._items.get(operation_id)
            if record is None:
                raise KeyError(operation_id)
            return record.model_copy(deep=True)

    def mark_running(self, operation_id: str) -> OperationRecord:
        """把 pending Operation 推进到 running；重复 running 幂等。"""
        with self._lock:
            record = self._require(operation_id)
            self._ensure_not_terminal(record)
            if record.state is OperationState.RUNNING:
                return record.model_copy(deep=True)
            if record.state is not OperationState.PENDING:
                raise ValueError(f"invalid operation transition from {record.state}")
            record.state = OperationState.RUNNING
            record.started_at = datetime.now(UTC)
            return record.model_copy(deep=True)

    def update_progress(self, operation_id: str, *, completed: int) -> OperationRecord:
        """更新已完成数量，并按 total 计算 0..1 进度。"""
        with self._lock:
            record = self._require(operation_id)
            self._ensure_not_terminal(record)
            if record.state is not OperationState.RUNNING:
                raise ValueError("operation must be running before progress update")
            if completed < 0 or (record.total and completed > record.total):
                raise ValueError("completed is outside operation total")
            record.completed = completed
            record.progress = completed / record.total if record.total else 0.0
            return record.model_copy(deep=True)

    def succeed(
        self, operation_id: str, result: dict[str, object] | None = None
    ) -> OperationRecord:
        """把 Operation 标记为 success。"""
        with self._lock:
            record = self._require(operation_id)
            self._ensure_not_terminal(record)
            record.state = OperationState.SUCCESS
            record.finished_at = datetime.now(UTC)
            record.result = result
            record.error = None
            if record.total:
                record.completed = record.total
                record.progress = 1.0
            return record.model_copy(deep=True)

    def complete_partial(
        self,
        operation_id: str,
        result: dict[str, object] | None = None,
    ) -> OperationRecord:
        """把 Operation 标记为 partial，供批量操作表达部分成功。"""
        with self._lock:
            record = self._require(operation_id)
            self._ensure_not_terminal(record)
            record.state = OperationState.PARTIAL
            record.finished_at = datetime.now(UTC)
            record.result = result
            return record.model_copy(deep=True)

    def cancel(self, operation_id: str) -> OperationRecord:
        """把 Operation 标记为 cancelled。"""
        with self._lock:
            record = self._require(operation_id)
            self._ensure_not_terminal(record)
            record.state = OperationState.CANCELLED
            record.finished_at = datetime.now(UTC)
            return record.model_copy(deep=True)

    def fail(
        self, operation_id: str, *, code: str, message: str,
        details: dict[str, object] | None = None,
    ) -> OperationRecord:
        """把 Operation 标记为 failed，并记录稳定错误码。"""
        with self._lock:
            record = self._require(operation_id)
            self._ensure_not_terminal(record)
            record.state = OperationState.FAILED
            record.finished_at = datetime.now(UTC)
            record.error = OperationError(
                code=code, message=message, details=details or {}
            )
            return record.model_copy(deep=True)

    @staticmethod
    def _ensure_not_terminal(record: OperationRecord) -> None:
        """终态不可再次推进，避免异步 worker 覆盖最终结果。"""
        if record.state in _TERMINAL_STATES:
            raise ValueError(f"operation '{record.operation_id}' is already terminal")

    def _require(self, operation_id: str) -> OperationRecord:
        """锁内获取真实记录对象；调用方负责持有 self._lock。"""
        record = self._items.get(operation_id)
        if record is None:
            raise KeyError(operation_id)
        return record
