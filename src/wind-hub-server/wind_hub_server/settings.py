"""wind-hub-server 进程级启动参数。

这里只保存服务宿主自身需要的参数，不复制 ``system.yaml`` 的业务配置。
现场设备、Task、Sink、ADS 等配置仍由 ``wind_hub.config`` 负责加载和校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
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
    collector_targets: tuple[str, ...] = ("127.0.0.1:50051",)
    commander_target: str = "127.0.0.1:50052"
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
        if not self.collector_targets:
            raise ValueError("collector_targets must not be empty")
        _ = self.collector_endpoints
        if not self.commander_target.strip():
            raise ValueError("commander_target must not be empty")
        if self.reconcile_interval <= 0:
            raise ValueError("reconcile_interval must be greater than 0")
        if self.worker_probe_interval <= 0:
            raise ValueError("worker_probe_interval must be greater than 0")

    @property
    def collector_endpoints(self) -> dict[str, str]:
        """返回 worker_id 到 endpoint 的映射，并校验多 Collector 定义。"""
        result: dict[str, str] = {}
        for raw in self.collector_targets:
            value = raw.strip()
            if not value:
                raise ValueError("collector target must not be empty")
            if "=" in value:
                worker_id, endpoint = (part.strip() for part in value.split("=", 1))
            else:
                if len(self.collector_targets) != 1:
                    raise ValueError(
                        "multiple collector targets require 'worker_id=host:port'"
                    )
                worker_id, endpoint = "collector", value
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

    @classmethod
    def from_values(
        cls,
        config_dir: str | Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        shutdown_timeout: float = 30.0,
        log_level: str = "info",
        collector_target: str | None = None,
        collector_targets: Sequence[str] | None = None,
        commander_target: str = "127.0.0.1:50052",
        reconcile_interval: float = 30.0,
        worker_probe_interval: float = 5.0,
    ) -> "ServerSettings":
        """从 CLI/调用方的基础值构造设置。

        Args:
            config_dir: 现场配置目录字符串或 Path。
            host: HTTP API 监听地址。
            port: HTTP API 监听端口。
            shutdown_timeout: Runtime 优雅停机硬超时。
            log_level: uvicorn 日志级别。

        Returns:
            已完成进程级参数校验的不可变设置。
        """
        return cls(
            config_dir=Path(config_dir),
            host=host,
            port=port,
            shutdown_timeout=shutdown_timeout,
            log_level=log_level,
            collector_targets=tuple(
                collector_targets
                if collector_targets is not None
                else (collector_target or "127.0.0.1:50051",)
            ),
            commander_target=commander_target,
            reconcile_interval=reconcile_interval,
            worker_probe_interval=worker_probe_interval,
        )
