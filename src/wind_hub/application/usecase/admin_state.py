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
        content = yaml.safe_dump(
            {"devices": rows}, allow_unicode=True, sort_keys=False
        )
        return await self._admin.apply_file(
            "devices.yaml",
            content,
            source="admin-devices",
            comment="Devices page structured update",
        )

    async def replace_tasks(self, items: list[AdminTaskItem]) -> ConfigApplyResult:
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
        content = yaml.safe_dump(
            {"tasks": rows}, allow_unicode=True, sort_keys=False
        )
        return await self._admin.apply_file(
            "tasks.yaml",
            content,
            source="admin-tasks",
            comment="Tasks page structured update",
        )

    async def replace_sinks(self, items: list[AdminSinkItem]) -> ConfigApplyResult:
        loaded = yaml.safe_load(self._admin.read_file("system.yaml")) or {}
        raw = cast(dict[str, Any], loaded)
        raw["sinks"] = [item.model_dump(mode="json") for item in items]
        content = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        return await self._admin.apply_file(
            "system.yaml",
            content,
            source="admin-sinks",
            comment="Sinks page structured update",
        )

    async def replace_definitions(
        self, state: AdminDefinitionsState
    ) -> ConfigApplyResult:
        files = {
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
        return await self._admin.apply_files(
            files,
            source="admin-definitions",
            comment="Definitions/Points structured update",
        )
