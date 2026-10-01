"""兼容进程入口——委托给 ``wind_hub_server``。

新部署应直接使用 ``wind-hub-server`` 或 ``python -m wind_hub_server``。
本模块保留 ``run_application`` / ``main`` 以及历史私有 helper 名称，
用于兼容现有 CLI、测试和外部调用；不再拥有独立的进程生命周期实现。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn

from wind_hub.application.usecase.config import ConfigUseCase
from wind_hub.assembly import AssembledRuntime
from wind_hub_server.server import (
    build_api_server,
    reload_once,
    run_server,
)
from wind_hub_server.settings import ServerSettings


def _build_api_server(rt: AssembledRuntime, host: str, port: int) -> uvicorn.Server:
    """兼容旧 helper；新代码使用 ``wind_hub_server.server.build_api_server``。"""
    settings = ServerSettings.from_values(".", host=host, port=port)
    return build_api_server(rt, settings)


async def _handle_sighup(config: ConfigUseCase) -> None:
    """兼容旧 helper；新代码使用 ``wind_hub_server.server.reload_once``。"""
    await reload_once(config)


async def run_application(
    config_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8080,
    shutdown_timeout: float = 30.0,
) -> int:
    """兼容旧进程 API，转发到 wind-hub-server。

    Args:
        config_dir: 现场配置目录。
        host: Web API 监听地址。
        port: Web API 监听端口。
        shutdown_timeout: Runtime 优雅停机整体硬超时。

    Returns:
        服务进程退出码。
    """
    settings = ServerSettings.from_values(
        config_dir,
        host=host,
        port=port,
        shutdown_timeout=shutdown_timeout,
    )
    return await run_server(settings)


def main() -> int:
    """``python -m wind_hub.main`` 的兼容同步入口。"""
    parser = argparse.ArgumentParser(
        prog="wind-hub.main",
        description="兼容入口；新部署推荐使用 wind-hub-server。",
    )
    parser.add_argument("--config", required=True, help="现场配置目录")
    parser.add_argument("--host", default="127.0.0.1", help="API 监听地址")
    parser.add_argument("--port", type=int, default=8080, help="API 监听端口")
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=30.0,
        help="优雅停机超时（秒）",
    )
    args = parser.parse_args()
    return asyncio.run(
        run_application(
            args.config,
            host=args.host,
            port=args.port,
            shutdown_timeout=args.shutdown_timeout,
        )
    )


def cli_entry() -> int:
    """兼容 :func:`main` 的外层脚本调用入口。"""
    return main()


if __name__ == "__main__":
    raise SystemExit(main())
