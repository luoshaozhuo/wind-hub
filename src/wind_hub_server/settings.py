"""wind-hub-server 进程级启动参数。

这里只保存服务宿主自身需要的参数，不复制 ``system.yaml`` 的业务配置。
现场设备、Task、Sink、ADS 等配置仍由 ``wind_hub_collector.config`` 负责加载和校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """服务进程启动参数。

    Args:
        config_dir: 现场配置目录。
        host: HTTP API 监听地址。
        port: HTTP API 监听端口，范围为 1..65535。
        shutdown_timeout: Runtime 优雅停机的整体硬超时，单位秒。
        log_level: 传递给 uvicorn 的日志级别。
    """

    config_dir: Path
    host: str = "127.0.0.1"
    port: int = 8080
    shutdown_timeout: float = 30.0
    log_level: str = "info"
    collectors: tuple[str, ...] = ("collector=127.0.0.1:50051",)
    commander: str = "127.0.0.1:50052"
    reconcile_interval: float = 30.0
    worker_probe_interval: float = 5.0

    def __post_init__(self) -> None:
        """校验仅属于进程宿主的参数，不读取文件系统。"""
        if not self.host.strip():
            raise ValueError("host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be greater than 0")
        if not self.log_level.strip():
            raise ValueError("log_level must not be empty")
        if not self.collectors:
            raise ValueError("collectors must not be empty")
        _ = self.collector_endpoints
        if not self.commander.strip():
            raise ValueError("commander must not be empty")
        if self.reconcile_interval <= 0:
            raise ValueError("reconcile_interval must be greater than 0")
        if self.worker_probe_interval <= 0:
            raise ValueError("worker_probe_interval must be greater than 0")

    @property
    def collector_endpoints(self) -> dict[str, str]:
        """返回 worker_id 到 endpoint 的映射，并校验多 Collector 定义。"""
        result: dict[str, str] = {}
        for raw in self.collectors:
            value = raw.strip()
            if not value:
                raise ValueError("collector target must not be empty")
            if "=" not in value:
                raise ValueError("collector must use 'worker_id=host:port'")
            worker_id, endpoint = (part.strip() for part in value.split("=", 1))
            if not worker_id:
                raise ValueError("collector worker_id must not be empty")
            if not endpoint:
                raise ValueError(
                    f"collector endpoint for '{worker_id}' must not be empty"
                )
            if worker_id in result:
                raise ValueError(f"duplicate collector worker_id '{worker_id}'")
            result[worker_id] = endpoint
        return result

