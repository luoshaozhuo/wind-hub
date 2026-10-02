"""Sink 管理、Verify 与 Write Test 用例。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast

import yaml
from pydantic import BaseModel, Field

from wind_hub.application.port.sink import SinkPort
from wind_hub.application.runtime.runtime import Runtime
from wind_hub_server.application.usecase.config import ConfigUseCase
from wind_hub_server.application.usecase.config_admin import ConfigAdminUseCase, ConfigApplyResult
from wind_hub.config.schema import SinkConfig
from wind_hub_core.model.point import PointValue


class SinkSnapshot(BaseModel):
    """Sink 配置与实时健康快照。"""

    name: str
    type: str
    enabled: bool
    params: dict[str, Any] = Field(default_factory=dict)
    healthy: bool
    message: str | None = None
    steps: list[dict[str, object]] = Field(default_factory=list)
    queue_depth: int = 0


class SinkTestResult(BaseModel):
    """Verify/Write Test 结果。"""

    success: bool
    latency_ms: float
    message: str | None = None


class SinkUseCase:
    """Sink 页面后端入口。"""

    def __init__(
        self,
        runtime: Runtime,
        config: ConfigUseCase,
        admin: ConfigAdminUseCase,
        factory: Callable[[SinkConfig], SinkPort],
    ) -> None:
        self._runtime = runtime
        self._config = config
        self._admin = admin
        self._factory = factory

    def list_sinks(self) -> list[SinkSnapshot]:
        """返回配置和 Runtime 健康状态。"""
        health = self._runtime.health()
        depths = self._runtime.sink_queue_depths()
        return [
            SinkSnapshot(
                name=cfg.name,
                type=cfg.type,
                enabled=cfg.enabled,
                params=dict(cfg.params),
                healthy=health.get(cfg.name).healthy if cfg.name in health else False,
                message=health.get(cfg.name).message if cfg.name in health else "not loaded",
                queue_depth=depths.get(cfg.name, 0),
            )
            for cfg in self._config.current_config.system.sinks
        ]

    def get_sink(self, name: str) -> SinkSnapshot:
        """查询单 Sink；不存在抛 KeyError。"""
        for row in self.list_sinks():
            if row.name == name:
                return row
        raise KeyError(name)

    async def verify(self, name: str) -> SinkTestResult:
        """用独立 Sink 实例执行 open/health/close，不干扰运行中的消费者。"""
        cfg = self._config_for(name)
        sink = self._factory(cfg)
        started = time.monotonic()
        steps: list[dict[str, object]] = []
        try:
            await sink.open()
            steps.append({"stage": "open", "success": True})
            health = sink.health()
            success = health.healthy
            message = health.message
            steps.append({"stage": "health", "success": success, "message": message})
        except Exception as exc:
            success = False
            message = str(exc) or type(exc).__name__
            steps.append({"stage": "open", "success": False, "message": message})
        finally:
            try:
                await sink.close()
            except Exception:
                pass
        return SinkTestResult(
            success=success,
            latency_ms=(time.monotonic() - started) * 1000,
            message=message,
            steps=steps,
        )

    async def write_test(self, name: str) -> SinkTestResult:
        """使用独立 Sink 实例写入一条明确标记的测试 PointValue。"""
        cfg = self._config_for(name)
        sink = self._factory(cfg)
        started = time.monotonic()
        steps: list[dict[str, object]] = []
        try:
            await sink.open()
            steps.append({"stage": "open", "success": True})
            await sink.write(
                [PointValue(device_id="_diagnostic", point_id="_write_test", value=1)]
            )
            steps.append({"stage": "write", "success": True})
            await sink.flush()
            steps.append({"stage": "flush", "success": True})
            success = True
            message = None
        except Exception as exc:
            success = False
            message = str(exc) or type(exc).__name__
            steps.append({"stage": "write", "success": False, "message": message})
        finally:
            try:
                await sink.close()
            except Exception:
                pass
        return SinkTestResult(
            success=success,
            latency_ms=(time.monotonic() - started) * 1000,
            message=message,
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
