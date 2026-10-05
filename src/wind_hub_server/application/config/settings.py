"""System Settings 的结构化配置服务。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from wind_hub_server.application.config.files import ConfigApplyResult, ConfigFileService
from wind_hub_server.application.config.service import ConfigService


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


class SettingsService:
    """Settings 与 system.yaml 的同源结构化入口。"""

    def __init__(self, config: ConfigService, admin: ConfigFileService) -> None:
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
        """只修改 system.yaml 对应字段，其余字段保持当前已提交内容。"""
        def mutate(documents: dict[str, dict[str, object]]) -> None:
            raw = documents["system.yaml"]
            raw["site"] = {"site_id": request.site_id, "name": request.site_name}
            interfaces = raw.setdefault("interfaces", {})
            if not isinstance(interfaces, dict):
                raise ValueError("system.yaml interfaces must be a mapping")
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

        return await self._admin.mutate_yaml_files(
            ("system.yaml",),
            mutate,
            source="settings",
            comment="System Settings update",
        )
