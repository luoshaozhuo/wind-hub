"""Shared Core Infrastructure。

这里只承载 Collector、Commander 等应用共同复用的基础设施实现。
"""

from .config import YamlFileCoreConfigRepository, YamlCoreConfigCodec

__all__ = [
    "FileCoreConfigRepository",
    "YamlCoreConfigCodec",
]
