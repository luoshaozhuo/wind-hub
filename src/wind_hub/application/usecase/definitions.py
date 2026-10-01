"""前端 Definitions/Metadata 页面使用的配置聚合读模型。"""

from __future__ import annotations

from typing import Any, cast

import yaml
from pydantic import BaseModel

from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.application.usecase.config_admin import ConfigAdminUseCase, ConfigApplyResult


class DefinitionsSnapshot(BaseModel):
    """当前已应用配置中的 definitions。"""

    units: dict[str, dict[str, Any]]
    device_types: dict[str, dict[str, Any]]
    device_models: dict[str, dict[str, Any]]
    point_tables: dict[str, dict[str, Any]]
    point_groups: list[str]
    device_groups: list[str]


class DefinitionsUseCase:
    """definitions 的只读聚合入口；修改统一经 Config Apply。"""

    def __init__(self, config: ConfigUseCase, admin: ConfigAdminUseCase) -> None:
        self._config = config
        self._admin = admin

    def snapshot(self) -> DefinitionsSnapshot:
        """从当前 Config 快照实时派生 definitions。"""
        cfg = self._config.current_config
        groups = sorted(
            {
                group
                for table in cfg.point_tables.tables.values()
                for point in table.points
                for group in point.point_groups
            }
        )
        models_raw = self._raw_mapping("device_models.yaml")
        return DefinitionsSnapshot(
            units=self._raw_mapping("units.yaml").get("units", {}),
            device_types=models_raw.get("device_types", {}),
            device_models=models_raw.get("device_models", {}),
            point_tables=self._raw_mapping("points.yaml").get("point_tables", {}),
            point_groups=groups,
            device_groups=sorted(
                {
                    device.device_group
                    for device in cfg.devices.devices
                    if device.device_group is not None
                }
            ),
        )

    async def upsert(
        self, kind: str, name: str, value: dict[str, Any]
    ) -> ConfigApplyResult:
        """结构化修改 definition，最终仍写回正式 YAML。"""
        file_name, root = self._location(kind)
        loaded = yaml.safe_load(self._admin.read_file(file_name)) or {}
        raw = cast(dict[str, Any], loaded)
        container = cast(dict[str, Any], raw.setdefault(root, {}))
        container[name] = value
        content = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        return await self._admin.apply_file(
            file_name,
            content,
            source="definitions",
            comment=f"upsert {kind} {name}",
        )

    async def delete(self, kind: str, name: str) -> ConfigApplyResult:
        """删除 definition；引用仍存在时完整配置校验会拒绝。"""
        file_name, root = self._location(kind)
        loaded = yaml.safe_load(self._admin.read_file(file_name)) or {}
        raw = cast(dict[str, Any], loaded)
        container = cast(dict[str, Any], raw.setdefault(root, {}))
        if name not in container:
            raise KeyError(name)
        del container[name]
        content = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        return await self._admin.apply_file(
            file_name,
            content,
            source="definitions",
            comment=f"delete {kind} {name}",
        )

    @staticmethod
    def _location(kind: str) -> tuple[str, str]:
        """映射 API definition kind 到唯一 YAML 根。"""
        mapping = {
            "units": ("units.yaml", "units"),
            "device-types": ("device_models.yaml", "device_types"),
            "device-models": ("device_models.yaml", "device_models"),
            "point-tables": ("points.yaml", "point_tables"),
        }
        location = mapping.get(kind)
        if location is None:
            raise KeyError(kind)
        return location


    def _raw_mapping(self, file_name: str) -> dict[str, Any]:
        """读取 Applied YAML 原始结构，保留 extends/remove_points 等编辑语义。"""
        loaded = yaml.safe_load(self._admin.read_file(file_name)) or {}
        return cast(dict[str, Any], loaded)
