"""Shared Core 配置持久化 outbound port。"""

from __future__ import annotations

from typing import Protocol

from core.domain import CoreConfigSnapshot


class ConfigRepositoryPort(Protocol):
    """共享配置加载与保存边界。"""

    async def load(self) -> CoreConfigSnapshot:
        ...

    async def save(self, snapshot: CoreConfigSnapshot) -> None:
        ...
