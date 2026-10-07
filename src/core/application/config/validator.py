"""Shared Core 可扩展配置校验端口。"""

from __future__ import annotations

from typing import Protocol

from .snapshot import CoreConfigSnapshot


class CoreConfigValidatorPort(Protocol):
    """配置激活前的附加静态校验能力。"""

    def validate(self, snapshot: CoreConfigSnapshot) -> None:
        """执行不产生外部 I/O 的静态校验。"""
        ...
