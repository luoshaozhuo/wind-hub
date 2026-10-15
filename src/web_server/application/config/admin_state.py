"""Admin 前端结构化配置状态的批量写入口。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from web_server.application.config.files import ConfigApplyResult, ConfigFileService


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
    interval: float | None = Field(default=None, gt=0)
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
            "sinks.yaml",
            "units.yaml",
            "device_models.yaml",
            "points.yaml",
        )

        def mutate(documents: dict[str, dict[str, Any]]) -> None:
            current_groups = documents["devices.yaml"].get("device_groups", {})
            if not isinstance(current_groups, dict):
                raise ValueError("devices.yaml device_groups must be a mapping")
            device_document = self._devices_document(devices)
            device_document["device_groups"] = {
                **current_groups,
                **{
                    group_id: {"name": group_id}
                    for group_id in device_document["device_groups"]
                    if group_id not in current_groups
                },
            }
            documents["devices.yaml"] = device_document
            documents["tasks.yaml"] = self._tasks_document(tasks)
            documents["sinks.yaml"] = {
                "sinks": {
                    item.name: {
                        "type": item.type,
                        "enabled": item.enabled,
                        "connection": item.connection,
                        "points": item.points,
                    }
                    for item in sinks
                }
            }
            # 单位目录包含 quantities 与 units 两个正式根，批量保存不得丢失物理量定义。
            definitions_documents = self._definition_documents(definitions)
            previous_units = documents["units.yaml"]
            definitions_documents["units.yaml"] = {
                **previous_units,
                "units": {
                    unit_id: {
                        **documents["units.yaml"].get("units", {}).get(unit_id, {}),
                        "symbol": item.get("symbol", ""),
                    }
                    for unit_id, item in definitions.units.items()
                },
            }
            documents.update(definitions_documents)

        return await self._admin.mutate_yaml_files(
            names,
            mutate,
            source="admin-state",
            comment="Admin structured state update",
        )

    @staticmethod
    def _devices_document(items: list[AdminDeviceItem]) -> dict[str, Any]:
        devices: dict[str, Any] = {}
        groups: set[str] = set()
        for item in items:
            if not item.device_group:
                raise ValueError(f"device {item.device_id} requires a device group")
            groups.add(item.device_group)
            endpoint = {"host": item.host}
            if item.port is not None:
                endpoint["port"] = item.port
            devices[item.device_id] = {
                "model_id": item.model,
                "device_group_ids": [item.device_group],
                "endpoint": endpoint,
                "protocol_options": dict(item.extensions),
                "enabled": item.enabled,
            }
        return {
            "devices": devices,
            "device_groups": {group: {"name": group} for group in sorted(groups)},
        }

    @staticmethod
    def _tasks_document(items: list[AdminTaskItem]) -> dict[str, Any]:
        tasks: dict[str, Any] = {}
        for item in items:
            if not item.device_group or item.device:
                raise ValueError(
                    f"task {item.task_id} requires device_group under current Core schema"
                )
            if item.interval is None or item.interval <= 0:
                raise ValueError(f"task {item.task_id} requires a positive interval")
            if not item.sinks:
                raise ValueError(f"task {item.task_id} requires at least one sink")
            tasks[item.task_id] = {
                "device_group_id": item.device_group,
                "point_group": item.point_group,
                "interval": item.interval,
                "sink_ids": list(item.sinks),
                "enabled": item.enabled,
            }
        return {"tasks": tasks}

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
