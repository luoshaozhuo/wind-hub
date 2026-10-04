"""Sink 管理、Verify 与 Write Test 用例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, Field

from wind_hub_core.config.sinks import ResolvedSinkConfig, SinkConfig
from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.port.monitoring import MonitoringSnapshotPort
from wind_hub_server.application.usecase.config import ConfigUseCase
from wind_hub_server.application.usecase.config_admin import ConfigAdminUseCase, ConfigApplyResult
from wind_hub_server.application.usecase.task_assignment import TaskAssignmentUseCase


class SinkSnapshot(BaseModel):
    """Sink 配置与实时健康快照。"""

    name: str
    type: str
    enabled: bool
    connection: dict[str, Any] = Field(default_factory=dict)
    points: list[dict[str, Any]] = Field(default_factory=list)
    point_count: int = 0
    healthy: bool
    message: str | None = None
    queue_depth: int = 0


class SinkTestResult(BaseModel):
    """Verify/Write Test 结果。"""

    success: bool
    latency_ms: float
    message: str | None = None
    steps: list[dict[str, object]] = Field(default_factory=list)


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


class SinkUseCase:
    """Sink 页面后端入口。"""

    def __init__(
        self,
        collectors: CollectorDirectory,
        monitoring: MonitoringSnapshotPort,
        assignments: TaskAssignmentUseCase,
        config: ConfigUseCase,
        admin: ConfigAdminUseCase,
    ) -> None:
        self._collectors = collectors
        self._monitoring = monitoring
        self._assignments = assignments
        self._config = config
        self._admin = admin

    def list_sinks(self) -> list[SinkSnapshot]:
        """返回配置与最近一次 Collector Sink 运行态。"""
        runtime_rows = self._monitoring.sinks_snapshot()
        runtime = {
            str(row.get("name")): row
            for row in runtime_rows
            if row.get("name") is not None
        }
        rows: list[SinkSnapshot] = []
        for cfg in self._config.current_config.sinks.sinks:
            current = runtime.get(cfg.name)
            rows.append(
                SinkSnapshot(
                    name=cfg.name,
                    type=cfg.type,
                    enabled=cfg.enabled,
                    connection=cfg.connection.model_dump(mode="json", by_alias=True),
                    points=[
                        point.model_dump(mode="json", by_alias=True)
                        for point in cfg.points
                    ],
                    point_count=len(cfg.points),
                    healthy=(
                        bool(current.get("healthy"))
                        if current is not None and cfg.enabled
                        else False
                    ),
                    message=(
                        str(current.get("message"))
                        if current is not None and current.get("message")
                        else ("disabled" if not cfg.enabled else "not loaded")
                    ),
                    queue_depth=(
                        _as_int(current.get("queue_depth"))
                        if current is not None
                        else 0
                    ),
                )
            )
        return rows

    def get_sink(self, name: str) -> SinkSnapshot:
        """查询单 Sink；不存在抛 KeyError。"""
        for row in self.list_sinks():
            if row.name == name:
                return row
        raise KeyError(name)

    async def verify(self, name: str) -> SinkTestResult:
        """由 Collector 在实际运行环境验证 Sink。"""
        self._config_for(name)
        started = time.monotonic()
        worker_ids = self._assignments.worker_ids_for_sink(name)
        if not worker_ids:
            return SinkTestResult(
                success=False,
                latency_ms=(time.monotonic() - started) * 1000,
                message="sink is not assigned to any collector",
            )
        results = await asyncio.gather(
            *(self._collectors.get(worker_id).verify_sink(name) for worker_id in worker_ids)
        )
        steps = [
            {
                "stage": "health",
                "worker_id": worker_id,
                "success": bool(result.get("success")),
                "message": result.get("message"),
            }
            for worker_id, result in zip(worker_ids, results, strict=True)
        ]
        return SinkTestResult(
            success=all(bool(result.get("success")) for result in results),
            latency_ms=(time.monotonic() - started) * 1000,
            message=next(
                (
                    str(result.get("message"))
                    for result in results
                    if result.get("message")
                ),
                None,
            ),
            steps=steps,
        )

    async def write_test(self, name: str) -> SinkTestResult:
        """由 Collector 对当前运行 Sink 执行明确标记的测试写入。"""
        self._config_for(name)
        started = time.monotonic()
        worker_ids = self._assignments.worker_ids_for_sink(name)
        if not worker_ids:
            return SinkTestResult(
                success=False,
                latency_ms=(time.monotonic() - started) * 1000,
                message="sink is not assigned to any collector",
            )
        results = await asyncio.gather(
            *(
                self._collectors.get(worker_id).write_test_sink(name)
                for worker_id in worker_ids
            )
        )
        steps = [
            {
                "stage": "write",
                "worker_id": worker_id,
                "success": bool(result.get("success")),
                "message": result.get("message"),
            }
            for worker_id, result in zip(worker_ids, results, strict=True)
        ]
        return SinkTestResult(
            success=all(bool(result.get("success")) for result in results),
            latency_ms=(time.monotonic() - started) * 1000,
            message=next(
                (
                    str(result.get("message"))
                    for result in results
                    if result.get("message")
                ),
                None,
            ),
            steps=steps,
        )

    async def upsert(self, name: str, payload: dict[str, Any]) -> ConfigApplyResult:
        """新增或更新 Sink；通过 sinks.yaml 配置事务生效。"""
        cfg = SinkConfig(name=name, **payload)

        def mutate(documents: dict[str, dict[str, Any]]) -> None:
            raw = documents["sinks.yaml"]
            current = raw.get("sinks") or []
            if not isinstance(current, list):
                raise ValueError("sinks.yaml sinks must be a list")
            sinks = [
                item
                for item in current
                if isinstance(item, dict) and item.get("name") != name
            ]
            sinks.append(cfg.model_dump(mode="json", by_alias=True, exclude_none=True))
            raw["sinks"] = sinks

        return await self._admin.mutate_yaml_files(
            ("sinks.yaml",),
            mutate,
            source="sinks",
            comment=f"upsert sink {name}",
        )

    async def delete(self, name: str) -> ConfigApplyResult:
        """删除 Sink；若 Task 仍引用它，正式配置校验会拒绝 Apply。"""
        self._config_for(name)

        def mutate(documents: dict[str, dict[str, Any]]) -> None:
            raw = documents["sinks.yaml"]
            current = raw.get("sinks") or []
            if not isinstance(current, list):
                raise ValueError("sinks.yaml sinks must be a list")
            raw["sinks"] = [
                item
                for item in current
                if isinstance(item, dict) and item.get("name") != name
            ]

        return await self._admin.mutate_yaml_files(
            ("sinks.yaml",),
            mutate,
            source="sinks",
            comment=f"delete sink {name}",
        )

    def _config_for(self, name: str) -> ResolvedSinkConfig:
        """从当前 resolved Config 取 Sink 定义。"""
        for cfg in self._config.current_config.sinks.sinks:
            if cfg.name == name:
                return cfg
        raise KeyError(name)
