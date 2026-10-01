"""System Settings 的结构化配置用例。"""

from __future__ import annotations

from typing import Any, cast

import yaml
from pydantic import BaseModel, Field

from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.application.usecase.config_admin import ConfigAdminUseCase, ConfigApplyResult


class SettingsSnapshot(BaseModel):
    """当前 system.yaml 中可由 Settings 页编辑的正式字段。"""

    site_id: str
    site_name: str | None = None
    api_enabled: bool
    api_host: str
    api_port: int
    ads_local_ip: str | None = None
    ads_local_ams_net_id: str | None = None
    ads_username: str = "Administrator"
    ads_password: str = ""


class SettingsUpdate(SettingsSnapshot):
    """Settings 保存请求。"""

    api_port: int = Field(ge=1, le=65535)


class SettingsUseCase:
    """Settings 与 system.yaml 的同源结构化入口。"""

    def __init__(self, config: ConfigUseCase, admin: ConfigAdminUseCase) -> None:
        self._config = config
        self._admin = admin

    def get(self) -> SettingsSnapshot:
        """从当前已应用 Config 生成 Settings。"""
        system = self._config.current_config.system
        site = system.site
        ads = system.ads
        api = system.interfaces.api
        return SettingsSnapshot(
            site_id=site.site_id if site is not None else "",
            site_name=site.name if site is not None else None,
            api_enabled=api.enabled,
            api_host=api.host,
            api_port=api.port,
            ads_local_ip=ads.local_ip if ads is not None else None,
            ads_local_ams_net_id=ads.local_ams_net_id if ads is not None else None,
            ads_username=ads.username if ads is not None else "Administrator",
            ads_password=ads.password if ads is not None else "",
        )

    async def update(self, request: SettingsUpdate) -> ConfigApplyResult:
        """只修改 system.yaml 对应字段，其余 runtime/sinks/cli 原样保留。"""
        loaded = yaml.safe_load(self._admin.read_file("system.yaml")) or {}
        raw = cast(dict[str, Any], loaded)
        raw["site"] = {"site_id": request.site_id, "name": request.site_name}
        interfaces = cast(dict[str, Any], raw.setdefault("interfaces", {}))
        interfaces["api"] = {
            "enabled": request.api_enabled,
            "host": request.api_host,
            "port": request.api_port,
        }
        if request.ads_local_ip and request.ads_local_ams_net_id:
            raw["ads"] = {
                "local_ip": request.ads_local_ip,
                "local_ams_net_id": request.ads_local_ams_net_id,
                "username": request.ads_username,
                "password": request.ads_password,
            }
        else:
            raw.pop("ads", None)
        content = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        return await self._admin.apply_file(
            "system.yaml", content, source="settings", comment="System Settings update"
        )
