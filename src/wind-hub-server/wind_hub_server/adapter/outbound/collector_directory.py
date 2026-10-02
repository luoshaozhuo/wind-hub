"""Server 进程内 Collector 目录。"""

from __future__ import annotations

from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.port.worker import CollectorPort


class StaticCollectorDirectory(CollectorDirectory):
    """静态 Collector 目录。

    当前由组合根一次性注册 Collector；不负责服务发现、健康判定、负载均衡或故障转移。
    """

    def __init__(
        self,
        collectors: dict[str, CollectorPort],
        *,
        default_worker_id: str,
    ) -> None:
        if not collectors:
            raise ValueError("collectors must not be empty")
        if default_worker_id not in collectors:
            raise ValueError(
                f"default worker '{default_worker_id}' is not registered"
            )
        self._collectors = dict(collectors)
        self._default_worker_id = default_worker_id

    @property
    def default_worker_id(self) -> str:
        """返回默认 Collector worker_id。"""
        return self._default_worker_id

    def get(self, worker_id: str) -> CollectorPort:
        """返回指定 Collector 出站端口。"""
        try:
            return self._collectors[worker_id]
        except KeyError as exc:
            raise KeyError(worker_id) from exc

    def list_worker_ids(self) -> list[str]:
        """返回稳定排序后的 Collector worker_id。"""
        return sorted(self._collectors)
