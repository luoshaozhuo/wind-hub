"""Admin 前端结构化配置状态的批量写入口。"""

from __future__ import annotations

from typing import Any, cast

import yaml
from pydantic import BaseModel, Field

from wind_hub.application.usecase.config_admin import ConfigAdminUseCase, ConfigApplyResult


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
    params: dict[str, Any] = Field(default_factory=dict)


class AdminDefinitionsState(BaseModel):
    units: dict[str, dict[str, Any]]
    device_types: dict[str, dict[str, Any]]
    device_models: dict[str, dict[str, Any]]
    point_tables: dict[str, dict[str, Any]]


class AdminStateUseCase:
    """把前端结构化编辑结果落回正式 YAML 配置集。"""

    def __init__(self, admin: ConfigAdminUseCase) -> None:
        self._admin = admin

    async def replace_devices(
        self, items: list[AdminDeviceItem]
    ) -> ConfigApplyResult:
        content = self._devices_yaml(items)
        return await self._admin.apply_file(
            "devices.yaml",
            content,
            source="admin-devices",
            comment="Devices page structured update",
        )

    async def replace_tasks(self, items: list[AdminTaskItem]) -> ConfigApplyResult:
        content = self._tasks_yaml(items)
        return await self._admin.apply_file(
            "tasks.yaml",
            content,
            source="admin-tasks",
            comment="Tasks page structured update",
        )

    async def replace_sinks(self, items: list[AdminSinkItem]) -> ConfigApplyResult:
        content = self._system_yaml_with_sinks(items)
        return await self._admin.apply_file(
            "system.yaml",
            content,
            source="admin-sinks",
            comment="Sinks page structured update",
        )

    async def replace_definitions(
        self, state: AdminDefinitionsState
    ) -> ConfigApplyResult:
        files = self._definition_files(state)
        return await self._admin.apply_files(
            files,
            source="admin-definitions",
            comment="Definitions/Points structured update",
        )


    async def replace_all(
        self,
        devices: list[AdminDeviceItem],
        tasks: list[AdminTaskItem],
        sinks: list[AdminSinkItem],
        definitions: AdminDefinitionsState,
    ) -> ConfigApplyResult:
        """一次事务提交前端结构化配置，解决跨文件引用更新。"""
        files = {
            "devices.yaml": self._devices_yaml(devices),
            "tasks.yaml": self._tasks_yaml(tasks),
            "system.yaml": self._system_yaml_with_sinks(sinks),
            **self._definition_files(definitions),
        }
        return await self._admin.apply_files(
            files,
            source="admin-state",
            comment="Admin structured state update",
        )

    @staticmethod
    def _devices_yaml(items: list[AdminDeviceItem]) -> str:
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
        return yaml.safe_dump(
            {"devices": rows}, allow_unicode=True, sort_keys=False
        )

    @staticmethod
    def _tasks_yaml(items: list[AdminTaskItem]) -> str:
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
        return yaml.safe_dump({"tasks": rows}, allow_unicode=True, sort_keys=False)

    def _system_yaml_with_sinks(self, items: list[AdminSinkItem]) -> str:
        loaded = yaml.safe_load(self._admin.read_file("system.yaml")) or {}
        raw = cast(dict[str, Any], loaded)
        raw["sinks"] = [item.model_dump(mode="json") for item in items]
        return yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _definition_files(state: AdminDefinitionsState) -> dict[str, str]:
        return {
            "units.yaml": yaml.safe_dump(
                {"units": state.units}, allow_unicode=True, sort_keys=False
            ),
            "device_models.yaml": yaml.safe_dump(
                {
                    "device_types": state.device_types,
                    "device_models": state.device_models,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            "points.yaml": yaml.safe_dump(
                {"point_tables": state.point_tables},
                allow_unicode=True,
                sort_keys=False,
            ),
        }
