"""Shared Core 配置导入/导出编码端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .snapshot import CoreConfigSnapshot


@dataclass(frozen=True, slots=True)
class CoreConfigArtifact:
    """一个可导入/导出的配置制品。

    content 可以是单 YAML、ZIP、多文件归档或其他实现定义的二进制表示；
    Application 只关心制品内容与媒体类型，不感知具体文件系统。
    """

    content: bytes
    media_type: str

    def __post_init__(self) -> None:
        media_type = self.media_type.strip().lower()
        if not media_type:
            raise ValueError("config artifact media_type must not be empty")
        object.__setattr__(self, "media_type", media_type)


class CoreConfigCodecPort(Protocol):
    """Shared Core 配置快照与外部制品之间的编解码边界。"""

    def encode(self, snapshot: CoreConfigSnapshot) -> CoreConfigArtifact:
        """把配置快照导出为可传输制品。"""
        ...

    def decode(self, artifact: CoreConfigArtifact) -> CoreConfigSnapshot:
        """把外部制品解析为 Shared Core 配置快照。"""
        ...
