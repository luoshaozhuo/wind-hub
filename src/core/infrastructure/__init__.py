"""Shared Core Infrastructure 公共门面。

只暴露跨应用装配真正需要的稳定 Adapter 与 Registry。
"""

from .config import YamlFileCoreConfigRepository
from .protocol import (
    ADSLocalConfig,
    ADSLocalRouter,
    ProtocolConfigValidator,
    ProtocolRegistry,
    build_protocol_registry,
)

__all__ = [
    "ADSLocalConfig",
    "ADSLocalRouter",
    "ProtocolConfigValidator",
    "ProtocolRegistry",
    "YamlFileCoreConfigRepository",
    "build_protocol_registry",
]
