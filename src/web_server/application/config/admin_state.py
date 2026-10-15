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
            for device_id, entry in device_document["devices"].items():
                previous = documents["devices.yaml"].get("devices", {}).get(device_id, {})
                if "name" in previous:
                    entry["name"] = previous["name"]
                previous_groups = previous.get("device_group_ids", [])
                if isinstance(previous_groups, list):
                    primary = entry["device_group_ids"][0]
                    entry["device_group_ids"].extend(
                        group for group in previous_groups[1:] if group != primary
                    )
            documents["devices.yaml"] = device_document
            documents["tasks.yaml"] = self._tasks_document(tasks)
            previous_sinks = documents["sinks.yaml"].get("sinks", {})
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
            # Redis 密码属于只写字段；UI 不回显时沿用同一个 Sink 的原值。
            for sink_id, entry in documents["sinks.yaml"]["sinks"].items():
                old_sink = previous_sinks.get(sink_id, {})
                if entry["type"] != "redis" or old_sink.get("type") != "redis":
                    continue
                old_connection = old_sink.get("connection", {})
                if "password" not in entry["connection"] and "password" in old_connection:
                    entry["connection"]["password"] = old_connection["password"]
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
            old_models = documents["device_models.yaml"].get("device_models", {})
            if not isinstance(old_models, dict):
                raise ValueError("device_models.yaml device_models must be a mapping")
            model_definitions = definitions_documents["device_models.yaml"]["device_models"]
            definitions_documents["device_models.yaml"]["device_models"] = {
                model_id: {**old_models.get(model_id, {}), **value}
                for model_id, value in model_definitions.items()
            }
            original_types = documents["device_models.yaml"].get("device_types", {})
            if not isinstance(original_types, dict):
                raise ValueError("device_models.yaml device_types must be a mapping")
            types = definitions_documents["device_models.yaml"]["device_types"]
            definitions_documents["device_models.yaml"]["device_types"] = {
                type_id: {**original_types.get(type_id, {}), **entry}
                for type_id, entry in types.items()
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
            endpoint: dict[str, Any] = {"host": item.host}
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
