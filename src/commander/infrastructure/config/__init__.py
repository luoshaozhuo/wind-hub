"""Commander 现场配置适配器（Infrastructure）。

解析与旧系统同一套现场配置目录的 Commander 子集，产出
领域配置索引与进程级 ``CommanderConfig``。
"""

from .loader import load_commander_config

__all__ = [
    "load_commander_config",
]
