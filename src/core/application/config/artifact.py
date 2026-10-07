"""Shared Core 配置交换制品值对象。"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ConfigError


@dataclass(frozen=True, slots=True)
class CoreConfigArtifact:
    """一个可导入/导出的配置制品。"""

    content: bytes
    media_type: str

    def __post_init__(self) -> None:
        media_type = self.media_type.strip().lower()
        if not media_type:
            raise ConfigError("config artifact media_type must not be empty")
        object.__setattr__(self, "media_type", media_type)
