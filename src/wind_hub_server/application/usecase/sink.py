"""Sink 管理、Verify 与 Write Test 用例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

import yaml
from pydantic import BaseModel, Field

from wind_hub_core.config.schema import SinkConfig
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
    params: dict[str, Any] = Field(default_factory=dict)
    healthy: bool
    message: str | None = None
    queue_depth: int = 0


class SinkTestResult(BaseModel):
    """Verify/Write Test 结果。"""

    success: bool
    latency_ms: float
    message: str | None = None
    steps: list[dict[str, object]] = Field(default_factory=list)


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
        for cfg in self._config.current_config.system.sinks:
            current = runtime.get(cfg.name)
            rows.append(
                SinkSnapshot(
                    name=cfg.name,
                    type=cfg.type,
                    enabled=cfg.enabled,
                    params=dict(cfg.params),
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
                        int(current.get("queue_depth") or 0)
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
        """新增或更新 Sink；最终仍通过 system.yaml + Config Apply 生效。"""
        cfg = SinkConfig(name=name, **payload)
        loaded = yaml.safe_load(self._admin.read_file("system.yaml")) or {}
        raw = cast(dict[str, Any], loaded)
        sinks = cast(list[dict[str, Any]], list(raw.get("sinks") or []))
        sinks = [item for item in sinks if item.get("name") != name]
        sinks.append(cfg.model_dump(mode="json"))
        raw["sinks"] = sinks
        content = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        return await self._admin.apply_file(
            "system.yaml", content, source="sinks", comment=f"upsert sink {name}"
        )

    async def delete(self, name: str) -> ConfigApplyResult:
        """删除 Sink；若 Task 仍引用它，正式配置校验会拒绝 Apply。"""
        self._config_for(name)
        loaded = yaml.safe_load(self._admin.read_file("system.yaml")) or {}
        raw = cast(dict[str, Any], loaded)
        raw["sinks"] = [
            item for item in list(raw.get("sinks") or []) if item.get("name") != name
        ]
        content = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        return await self._admin.apply_file(
            "system.yaml", content, source="sinks", comment=f"delete sink {name}"
        )

    def _config_for(self, name: str) -> SinkConfig:
        """从当前 Config 取 SinkConfig。"""
        for cfg in self._config.current_config.system.sinks:
            if cfg.name == name:
                return cfg
        raise KeyError(name)
