"""输出 Sink 的共享静态配置语义。"""

from __future__ import annotations

from dataclasses import dataclass

from .identities import SinkId


@dataclass(frozen=True, slots=True)
class SinkDefinition:
    """Application 可引用的 Sink 静态定义。

    本对象只保存跨组件共享的稳定语义：Sink 身份、类型与启用状态。
    连接参数、协议地址、文件路径、数据库 DSN 等 Adapter 专有配置不进入
    Shared Core，由具体 Sink Adapter 的配置模型负责。
    """

    sink_id: SinkId
    sink_type: str
    enabled: bool = True

    def __post_init__(self) -> None:
        sink_id = self.sink_id.strip()
        sink_type = self.sink_type.strip().lower()

        if not sink_id:
            raise ValueError("sink_id must not be empty")
        if not sink_type:
            raise ValueError("sink_type must not be empty")

        object.__setattr__(self, "sink_id", SinkId(sink_id))
        object.__setattr__(self, "sink_type", sink_type)
