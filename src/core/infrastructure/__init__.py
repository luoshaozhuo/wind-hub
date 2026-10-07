"""Shared Core Infrastructure 公共门面。

只暴露跨应用装配真正需要的稳定 Adapter 与 Registry。
"""

from .config import (
    YamlCoreConfigCodec,
    YamlFileCoreConfigRepository,
    fingerprint_core_config,
)
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
    "YamlCoreConfigCodec",
    "YamlFileCoreConfigRepository",
    "build_protocol_registry",
    "fingerprint_core_config",
]
