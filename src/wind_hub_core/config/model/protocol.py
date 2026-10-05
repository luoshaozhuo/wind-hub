"""配置域共享的协议词汇。

设备型号、设备实例与点表都以协议 ID（``ads`` / ``modbus`` / ``iec104``）
引用协议驱动能力；该集合是配置域内跨模型共享的稳定词汇。
"""

from __future__ import annotations

SUPPORTED_PROTOCOLS = frozenset({"ads", "modbus", "iec104"})
"""现有协议驱动支持的协议集合。"""


__all__ = [
    "SUPPORTED_PROTOCOLS",
]
