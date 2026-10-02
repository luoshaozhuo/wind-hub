"""IEC 60870-5-104 Driver 配置模型。

参数来自 DeviceConfig.endpoint/extensions；本模块只做配置转换，不建立 TCP
连接，也不启动 t1/t2/t3 timer。
"""

from __future__ import annotations

from dataclasses import dataclass

from wind_hub.config.schema import DeviceConfig


@dataclass(frozen=True)
class IEC104Config:
    """IEC104 单设备连接与流控参数。"""

    host: str
    """IEC104 从站 IP 或主机名。"""

    port: int
    """TCP 端口，通常为 2404。"""

    common_addr: int
    """公共地址 CASDU。"""

    k: int
    """发送窗口 k：允许未确认的最大 I-frame 数。"""

    w: int
    """接收窗口 w：达到该数量时必须发送 S-frame 确认。"""

    t0: float
    """连接建立超时 t0，单位秒。"""

    t1: float
    """发送/确认超时 t1；I-frame 在该时间内未确认则认为连接异常。"""

    t2: float
    """延迟确认超时 t2；未提前达到 w 时，到期发送 S-frame。"""

    t3: float
    """空闲保活超时 t3；到期触发 TESTFR。"""

    max_reconnect_retries: int = 5
    """连接失败后的最大连续重试次数。"""

    @classmethod
    def from_device_config(cls, cfg: DeviceConfig) -> IEC104Config:
        """从 DeviceConfig 构造 IEC104Config。

        Args:
            cfg: 已解析的设备配置。

        Returns:
            使用 endpoint/extensions 和协议默认值构造的 IEC104Config。
        """
        extensions = cfg.endpoint.extensions

        return cls(
            host=cfg.endpoint.host,
            port=cfg.endpoint.port,
            common_addr=int(extensions.get("common_addr", 1)),
            k=int(extensions.get("k", 12)),
            w=int(extensions.get("w", 8)),
            t0=float(extensions.get("t0", 30.0)),
            t1=float(extensions.get("t1", 15.0)),
            t2=float(extensions.get("t2", 10.0)),
            t3=float(extensions.get("t3", 20.0)),
            max_reconnect_retries=int(extensions.get("max_reconnect_retries", 5)),
        )
