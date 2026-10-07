"""Shared Core Infrastructure。

这里只承载 Collector、Commander 等应用共同复用的基础设施实现。
"""

from .config import YamlCoreConfigCodec, YamlFileCoreConfigRepository
from .protocol import ProtocolFactory, ProtocolRegistry

__all__ = [
    "ProtocolFactory",
    "ProtocolRegistry",
    "YamlCoreConfigCodec",
    "YamlFileCoreConfigRepository",
]
