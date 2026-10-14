"""Collector Worker 目录端口。"""

from __future__ import annotations

from typing import Protocol

from wind_hub_server.application.port.worker import CollectorPort


class CollectorDirectory(Protocol):
    """按 Server 逻辑 worker_id 定位 Collector 出站端口。"""

    def get(self, worker_id: str) -> CollectorPort:
        """返回指定 Collector；未知 worker_id 抛 KeyError。"""
        ...

    def list_worker_ids(self) -> list[str]:
        """返回已登记 Collector worker_id。"""
        ...
