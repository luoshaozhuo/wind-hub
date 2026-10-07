"""Commander 现场配置适配器（Infrastructure）。

解析与旧系统同一套现场配置目录的 Commander 子集，产出
``CoreConfigSnapshot`` 与进程级 ``CommanderConfig``。
"""

from .fingerprint import fingerprint_config_set
from .loader import load_commander_config

__all__ = [
    "fingerprint_config_set",
    "load_commander_config",
]
