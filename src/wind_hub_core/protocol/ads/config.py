"""ADS 连接参数模型与 DeviceConfig 转换。

目标 IP 来自 DeviceConfig.endpoint.host，因此不重复存入 ADSConfig。该模块只
负责参数解析和校验，不建立 ADS connection，也不设置进程级本机 AMS Net ID。
"""

from __future__ import annotations

from dataclasses import dataclass

from wind_hub_core.config import DeviceConfig
from wind_hub_core.model.errors import ConfigError


@dataclass(frozen=True)
class ADSConfig:
    """ADS 设备连接与读取参数。

    这些字段只描述单设备目标端；本机 AMS Net ID 属于进程级 system 配置。
    """

    target_net_id: str
    """目标 PLC AMS Net ID。

    未配置时为空，由 driver/pyads 根据连接上下文处理；本机 AMS Net ID 不属于
    单设备配置。
    """

    target_port: int = 801
    """目标 AMS port；项目约定 TwinCAT 2 默认 801，TwinCAT 3 默认 802。"""

    timeout: float = 5.0
    """单次 ADS 操作超时，单位秒。"""

    twincat_version: str = "2"
    """TwinCAT runtime 版本，仅允许 2 或 3；用于配置/UI 语义，不替代 AMS port。"""

    read_mode: str = "sum"
    """批量读取策略：sum 使用 ADS Sum Read；sequential 逐点读取并限制并发。"""

    max_subs_per_sum: int = 500
    """单次 ADS Sum Read 允许的最大子命令数，超过时分块。"""

    max_concurrent_reads: int = 16
    """sequential 模式并发读取上限。"""

    subscribe_enabled: bool = False
    """是否启用 ADS notification 订阅采集。"""

    max_delay: float = 0.06
    """notification 最大允许延迟，单位秒。"""

    max_notifications_per_connection: int = 550
    """单条订阅 connection 允许的 notification handle 上限。"""


def _is_valid_ams_net_id(net_id: str) -> bool:
    """校验六段十进制 AMS Net ID 格式。

    Returns:
        格式为 a.b.c.d.e.f 且每段在 0~255 时返回 True。
    """
    parts = net_id.split(".")
    if len(parts) != 6:
        return False
    return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def from_device_config(cfg: DeviceConfig) -> ADSConfig:
    """从 DeviceConfig 构造并校验 ADSConfig。

    Args:
        cfg: 已通过基础 schema 校验的设备配置。

    Returns:
        解析后的 ADSConfig。

    Raises:
        ConfigError: target_net_id 或 twincat_version 非法。
    """
    ext = cfg.endpoint.extensions

    target_net_id = str(ext.get("target_net_id", ""))
    if target_net_id and not _is_valid_ams_net_id(target_net_id):
        raise ConfigError(
            f"ADS device '{cfg.device_id}': invalid target_net_id '{target_net_id}'; "
            f"expected dotted-numeric 'a.b.c.d.e.f'"
        )

    twincat_version = str(ext.get("twincat_version", "2"))
    if twincat_version not in ("2", "3"):
        raise ConfigError(
            f"ADS device '{cfg.device_id}': twincat_version must be '2' or '3', "
            f"got '{twincat_version}'"
        )
    return ADSConfig(
        target_net_id=target_net_id,
        target_port=cfg.endpoint.port,
        timeout=float(ext.get("timeout", 5.0)),
        twincat_version=twincat_version,
        read_mode=cfg.read_mode,
        max_subs_per_sum=int(ext.get("max_subs_per_sum", 500)),
        max_concurrent_reads=int(ext.get("max_concurrent_reads", 16)),
        # 订阅开关与协议参数由 endpoint.extensions 透传；notification 的
        # cycle_time 不再是设备级配置——采集节拍属于 Task（Task.interval），
        # 订阅建立时由调用方传入。
        subscribe_enabled=bool(ext.get("subscribe_enabled", False)),
        max_delay=float(ext.get("max_delay", 0.06)),
        max_notifications_per_connection=int(ext.get("max_notifications_per_connection", 550)),
    )
