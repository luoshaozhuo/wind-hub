"""Sink 管理与独立服务状态查询。"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from web_server.application.config.files import ConfigApplyResult, ConfigFileService
from web_server.application.config.service import ConfigService
from web_server.application.config.sink import Sink
from web_server.application.port.monitoring import MonitoringSnapshotPort
from web_server.application.port.sink import SinkDirectoryPort
from web_server.application.sink.yaml_values import yaml_plain


class SinkSnapshot(BaseModel):
    """Sink 配置与实时健康快照。"""

    name: str
    type: str
    enabled: bool
    connection: dict[str, Any] = Field(default_factory=dict)
    healthy: bool
    message: str | None = None
    queue_depth: int = 0
    has_password: bool = False


class SinkTestResult(BaseModel):
    """Sink 状态验证结果。"""

    success: bool
    latency_ms: float
    message: str | None = None
    steps: list[dict[str, object]] = Field(default_factory=list)


class SinkService:
    """Sink 页面后端入口。"""

    def __init__(
        self,
        monitoring: MonitoringSnapshotPort,
        sink_directory: SinkDirectoryPort,
        config: ConfigService,
        admin: ConfigFileService,
    ) -> None:
        self._monitoring = monitoring
        self._sink_directory = sink_directory
        self._config = config
        self._admin = admin

    def list_sinks(self) -> list[SinkSnapshot]:
        """返回配置与独立 Sink Service 的最近运行态。"""
        runtime = {row.name: row for row in self._monitoring.sinks_snapshot()}
        rows: list[SinkSnapshot] = []
        for cfg in self._config.current_config.sinks.values():
            current = runtime.get(cfg.sink_id)
            plain_connection = yaml_plain(cfg.connection)
            rows.append(
                SinkSnapshot(
                    name=cfg.sink_id,
                    type=cfg.kind,
                    enabled=cfg.enabled,
                    connection={
                        key: value for key, value in plain_connection.items() if key != "password"
                    },
                    has_password=bool(plain_connection.get("password")),
                    healthy=(current.healthy if current is not None and cfg.enabled else False),
                    message=(
                        current.message
                        if current is not None and current.message
                        else ("disabled" if not cfg.enabled else "not loaded")
                    ),
                    queue_depth=current.queue_depth if current is not None else 0,
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
        """Verify live Deliveries; a physical Sink has no independent status RPC."""
        self._config_for(name)
        started = time.monotonic()
        try:
            rows = await self._sink_directory.list_deliveries()
            members = [row for row in rows if row.get("sink_id") == name]
            healthy = bool(
                members
                and all(
                    row.get("desired_state") == "START"
                    and row.get("actual_state") == "RUNNING"
                    and not row.get("last_error")
                    for row in members
                )
            )
            state = ", ".join(
                sorted({str(row.get("actual_state", "UNKNOWN")) for row in members})
            )
            return SinkTestResult(
                success=healthy,
                latency_ms=(time.monotonic() - started) * 1000,
                message=state if members else "No active Delivery references this Sink",
            )
        except Exception as exc:
            return SinkTestResult(
                success=False,
                latency_ms=(time.monotonic() - started) * 1000,
                message=f"Sink Service unavailable: {exc}",
            )

    async def manage(self, name: str, operation: str) -> dict[str, Any]:
        """Control each Delivery through the official revision-checked RPC."""
        self._config_for(name)
        if operation not in {"start", "stop"}:
            raise ValueError("only start and stop have a safe official Delivery RPC")
        rows = await self._sink_directory.list_deliveries()
        members = sorted(
            (row for row in rows if row.get("sink_id") == name),
            key=lambda row: str(row["delivery_id"]),
        )
        if not members:
            raise RuntimeError(f"no Delivery references Sink {name}")
        results: list[dict[str, Any]] = []
        for row in members:
            identifier = str(row["delivery_id"])
            results.append(
                await self._sink_directory.control_delivery(
                    identifier, start=operation != "stop"
                )
            )
        return {"sink_id": name, "operation": operation, "results": results}

    async def upsert(self, name: str, payload: dict[str, Any]) -> ConfigApplyResult:
        """新增或更新 Sink；通过 sinks.yaml 配置事务生效。"""
        cfg = Sink(
            sink_id=name,
            kind=payload["type"],
            enabled=payload.get("enabled", True),
            connection=payload["connection"],
        )

        def mutate(documents: dict[str, dict[str, Any]]) -> None:
            raw = documents["sinks.yaml"]
            current = raw.setdefault("sinks", {})
            if not isinstance(current, dict):
                raise ValueError("sinks.yaml sinks must be a mapping")
            connection = yaml_plain(cfg.connection)
            previous = current.get(name, {})
            if cfg.kind in {"redis", "tdengine"} and previous.get("type") == cfg.kind:
                old_connection = previous.get("connection", {})
                if "password" not in connection:
                    # 只写字段缺省：UI 未回显密码，沿用原值。
                    if old_connection.get("password"):
                        connection["password"] = old_connection["password"]
                elif not connection.get("password"):
                    # 显式清空（null/""）：删除凭据键；需密码的连接由配置校验拒绝。
                    connection.pop("password", None)
            current[name] = {
                "type": cfg.kind,
                "enabled": cfg.enabled,
                "connection": connection,
                "outputs": previous.get("outputs", {}),
            }

        return await self._admin.mutate_yaml_files(
            ("sinks.yaml",),
            mutate,
            source="sinks",
            comment=f"upsert sink {name}",
        )

    async def delete(self, name: str) -> ConfigApplyResult:
        """删除 Sink 定义及其所属 Task 输出映射。"""
        self._config_for(name)

        def mutate(documents: dict[str, dict[str, Any]]) -> None:
            raw = documents["sinks.yaml"]
            current = raw.get("sinks")
            if not isinstance(current, dict):
                raise ValueError("sinks.yaml sinks must be a mapping")
            del current[name]

        return await self._admin.mutate_yaml_files(
            ("sinks.yaml",),
            mutate,
            source="sinks",
            comment=f"delete sink {name}",
        )

    def _config_for(self, name: str) -> Sink:
        """从当前 resolved Config 取 Sink 定义。"""
        for cfg in self._config.current_config.sinks.values():
            if cfg.sink_id == name:
                return cfg
        raise KeyError(name)
