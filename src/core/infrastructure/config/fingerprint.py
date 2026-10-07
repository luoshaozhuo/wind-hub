"""Shared Core 配置语义指纹。"""

from __future__ import annotations

import hashlib

from core.domain import CoreConfigSnapshot

from .yaml_codec import YamlCoreConfigCodec


def fingerprint_core_config(snapshot: CoreConfigSnapshot) -> str:
    """计算 Shared Core 配置的稳定语义指纹。

    指纹基于规范化 YAML Codec 的确定性输出，不依赖原始文件格式、字段顺序或
    空白。它用于配置分发/激活身份；Repository revision 仍用于持久化 CAS。
    """
    artifact = YamlCoreConfigCodec().encode(snapshot)
    return hashlib.sha256(artifact.content).hexdigest()
