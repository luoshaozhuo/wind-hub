"""System Settings 的结构化配置服务。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from web_server.application.config.files import ConfigApplyResult, ConfigFileService
from web_server.application.config.service import ConfigService


class SettingsValidationError(ValueError):
    """Settings 请求与部署边界冲突。"""


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

    def __init__(
        self,
        config: ConfigService,
        admin: ConfigFileService,
        *,
        api_host: str = "127.0.0.1",
        api_port: int = 8080,
    ) -> None:
        self._config = config
        self._admin = admin
        self._api_host = api_host
        self._api_port = api_port

    def get(self) -> SettingsSnapshot:
        """从当前已应用 Config 生成 Settings。"""
        system = self._config.current_config.system
        site = self._config.current_config.site
        ads = system.ads
        return SettingsSnapshot(
            site_id=site.site_id,
            site_name=site.name,
            api_enabled=True,
            api_host=self._api_host,
            api_port=self._api_port,
            ads_local_ip=ads.local_ip if ads is not None else None,
            ads_local_ams_net_id=ads.local_ams_net_id if ads is not None else None,
            ads_username="Administrator",
            ads_password="",
        )

    async def update(self, request: SettingsUpdate) -> ConfigApplyResult:
        """只修改 system.yaml 对应字段，其余字段保持当前已提交内容。"""
        listener = (request.api_enabled, request.api_host, request.api_port)
        if listener != (True, self._api_host, self._api_port):
            raise SettingsValidationError("API listener options belong to process deployment")
        if request.ads_password or request.ads_username != "Administrator":
            raise SettingsValidationError(
                "ADS device credentials must be configured per device protocol_options"
            )

        def mutate(documents: dict[str, dict[str, object]]) -> None:
            raw = documents["system.yaml"]
            raw["site"] = {
                "site_id": request.site_id,
                "name": request.site_name or request.site_id,
            }
            if bool(request.ads_local_ip) != bool(request.ads_local_ams_net_id):
                raise SettingsValidationError("ADS local identity requires both IP and AMS Net ID")
            if request.ads_local_ip and request.ads_local_ams_net_id:
                raw["ads"] = {
                    "local_ip": request.ads_local_ip,
                    "local_ams_net_id": request.ads_local_ams_net_id,
                }
            else:
                raw.pop("ads", None)

        return await self._admin.mutate_yaml_files(
            ("system.yaml",),
            mutate,
            source="settings",
            comment="System Settings update",
        )
