"""Admin 前端结构化配置状态的批量写入口。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from wind_hub_server.application.config.files import ConfigApplyResult, ConfigFileService


class AdminDeviceItem(BaseModel):
    device_id: str
    model: str
    device_group: str | None = None
    host: str
    port: int | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AdminTaskItem(BaseModel):
    task_id: str
    device: str | None = None
    device_group: str | None = None
    point_group: str
    interval: float | None = None
    sinks: list[str] = Field(default_factory=list)
    enabled: bool = True


class AdminSinkItem(BaseModel):
    name: str
    type: str
    enabled: bool = True
    connection: dict[str, Any]
    points: list[dict[str, Any]] = Field(default_factory=list)


class AdminDefinitionsState(BaseModel):
    units: dict[str, dict[str, Any]]
    device_types: dict[str, dict[str, Any]]
    device_models: dict[str, dict[str, Any]]
    point_tables: dict[str, dict[str, Any]]


class AdminStateService:
    """把前端结构化编辑结果落回正式 YAML 配置集。"""

    def __init__(self, admin: ConfigFileService) -> None:
        self._admin = admin

    async def replace_all(
        self,
        devices: list[AdminDeviceItem],
        tasks: list[AdminTaskItem],
        sinks: list[AdminSinkItem],
        definitions: AdminDefinitionsState,
    ) -> ConfigApplyResult:
        """一次事务提交前端结构化配置，解决跨文件引用更新。"""
        names = (
            "devices.yaml",
            "tasks.yaml",
            "system.yaml",
            "sinks.yaml",
            "units.yaml",
            "device_models.yaml",
            "points.yaml",
        )

        def mutate(documents: dict[str, dict[str, Any]]) -> None:
            documents["devices.yaml"] = self._devices_document(devices)
            documents["tasks.yaml"] = self._tasks_document(tasks)
            documents["sinks.yaml"] = {
                "sinks": [item.model_dump(mode="json") for item in sinks]
            }
            documents.update(self._definition_documents(definitions))

        return await self._admin.mutate_yaml_files(
            names,
            mutate,
            source="admin-state",
            comment="Admin structured state update",
        )

    @staticmethod
    def _devices_document(items: list[AdminDeviceItem]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for item in items:
            endpoint: dict[str, Any] = {"host": item.host}
            if item.port is not None:
                endpoint["port"] = item.port
            if item.extensions:
                endpoint["extensions"] = dict(item.extensions)
            row: dict[str, Any] = {
                "device_id": item.device_id,
                "model": item.model,
                "enabled": item.enabled,
                "endpoint": endpoint,
            }
            if item.device_group:
                row["device_group"] = item.device_group
            rows.append(row)
        return {"devices": rows}

    @staticmethod
    def _tasks_document(items: list[AdminTaskItem]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for item in items:
            row: dict[str, Any] = {
                "task_id": item.task_id,
                "point_group": item.point_group,
                "targets": [{"sink": name} for name in item.sinks],
                "enabled": item.enabled,
            }
            if item.device:
                row["device"] = item.device
            elif item.device_group:
                row["device_group"] = item.device_group
            if item.interval is not None:
                row["interval"] = item.interval
            rows.append(row)
        return {"tasks": rows}

    @staticmethod
    def _definition_documents(
        state: AdminDefinitionsState,
    ) -> dict[str, dict[str, Any]]:
        return {
            "units.yaml": {"units": state.units},
            "device_models.yaml": {
                "device_types": state.device_types,
                "device_models": state.device_models,
            },
            "points.yaml": {"point_tables": state.point_tables},
        }
