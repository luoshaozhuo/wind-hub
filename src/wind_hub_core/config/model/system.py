"""system.yaml 系统级配置模型。

进程级 ADS 本机身份、现场标识、跨进程共享 Runtime 参数与 Server 入站接口
设置。只做纯 schema 与局部校验，不创建任何可执行组件。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wind_hub_core.model.errors import ConfigError


def _validate_ams_net_id(net_id: str) -> str:
    """校验六段十进制 AMS Net ID。

    Args:
        net_id: 待校验 AMS Net ID。

    Returns:
        格式和每段范围都合法时返回原值。

    Raises:
        ValueError: 格式非法。
    """
    parts = net_id.split(".")
    if len(parts) != 6 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        raise ConfigError(
            f"Invalid AMS Net ID '{net_id}'; expected dotted-numeric 'a.b.c.d.e.f'"
        )
    return net_id


class ADSSystemConfig(BaseModel):
    """进程级 ADS 本机身份与凭据。"""

    model_config = ConfigDict(extra="forbid")

    local_ams_net_id: str
    """本机 AMS Net ID。"""

    local_ip: str
    """本机 IP。"""

    username: str = "Administrator"
    """ADS 管理用户名；供显式路由/诊断工具使用，不做自动 route repair。"""

    password: str = ""
    """ADS 管理密码，默认空。"""

    @field_validator("local_ams_net_id")
    @classmethod
    def _check_local_ams_net_id(cls, v: str) -> str:
        return _validate_ams_net_id(v)


class SiteConfig(BaseModel):
    """当前部署实例所属现场的身份（``system.yaml`` 的 ``site`` 段）。

    一个 wind-hub 运行实例只对应一个现场；不存在多现场嵌套配置。
    """

    model_config = ConfigDict(extra="forbid")

    site_id: str
    """现场唯一标识（如 ``'wind_farm_a'``）。"""

    name: str | None = None
    """现场显示名（如 ``'某某风电场'``）。"""

    @field_validator("site_id")
    @classmethod
    def _check_site_id(cls, v: str) -> str:
        if not v.strip():
            raise ConfigError("site_id must be non-empty")
        return v


class RuntimeConfig(BaseModel):
    """跨进程共享的运行参数。"""

    model_config = ConfigDict(extra="forbid")

    queue_maxsize: int = Field(default=1000, ge=1)
    """每个 Sink queue 的容量；满时按 backpressure_policy 处理。"""

    backpressure_policy: str = "drop_old"
    """Sink queue 满时的背压策略。

    drop_new 丢弃新批次；drop_old 淘汰旧批次；block 阻塞采集直到队列有空间。
    """

    shutdown_timeout: float = Field(default=30.0, gt=0)
    """优雅停机等待在途操作完成的最大时间，单位秒。"""

    connect_timeout: float = Field(default=10.0, gt=0)
    """单设备连接超时，单位秒。"""

    read_timeout: float = Field(default=5.0, gt=0)
    """单次批量读的应用层兜底超时，单位秒；协议 Driver 内部仍保留底层超时。"""

    write_timeout: float = Field(default=5.0, gt=0)
    """默认写超时，单位秒；Command.timeout <= 0 时由 CommandDispatcher 使用。"""

    @model_validator(mode="after")
    def _validate_backpressure(self) -> RuntimeConfig:
        allowed = {"drop_old", "drop_new", "block"}
        if self.backpressure_policy not in allowed:
            raise ConfigError(
                f"Invalid backpressure_policy '{self.backpressure_policy}'; "
                f"must be one of {sorted(allowed)}"
            )
        return self


class ApiConfig(BaseModel):
    """Server HTTP API 入站设置。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


class InterfaceConfig(BaseModel):
    """HTTP Server 入站适配器设置。"""

    model_config = ConfigDict(extra="forbid")

    api: ApiConfig = Field(default_factory=ApiConfig)


class SystemConfig(BaseModel):
    """system.yaml 顶层配置。"""

    model_config = ConfigDict(extra="forbid")

    site: SiteConfig | None = None
    """当前现场身份；``None`` 表示未声明（仅标识用途，不影响运行链路）。"""
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    ads: ADSSystemConfig | None = None
    """进程级 ADS 本机配置；为 ``None`` 时（或无 ADS 设备）不做 ADS 本机初始化。"""
    interfaces: InterfaceConfig = Field(default_factory=InterfaceConfig)


__all__ = [
    "ADSSystemConfig",
    "SiteConfig",
    "RuntimeConfig",
    "ApiConfig",
    "InterfaceConfig",
    "SystemConfig",
]
