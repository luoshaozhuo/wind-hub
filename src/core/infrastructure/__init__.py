"""Shared Core Infrastructure 公共门面。

只暴露共享协议基础设施。
"""

from .protocol import ADSLocalConfig, ADSLocalRouter

__all__ = [
    "ADSLocalConfig",
    "ADSLocalRouter",
]
