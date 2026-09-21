"""``wind-hub run`` — 前台启动引擎并阻塞，直到收到信号停机。

委托给 :func:`wind_hub.main.run_engine` 完成「装配 → 启动 → 等待
SIGINT/SIGTERM → 优雅停机」。本命令只负责把 typer 参数转发给入口函数。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from wind_hub.main import run_engine

app = typer.Typer(name="run", help="启动引擎（前台阻塞，由 systemd/supervisor 管理）")


@app.callback(invoke_without_command=True)
def run(
    config: Path = typer.Option(
        ..., "--config", help="配置目录（含 system/devices/points/routing.yaml）"
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="API 监听地址"),
    port: int = typer.Option(8080, "--port", help="API 监听端口"),
    shutdown_timeout: float = typer.Option(30.0, "--shutdown-timeout", help="优雅停机超时（秒）"),
) -> None:
    """启动引擎并前台阻塞，等待 SIGINT/SIGTERM 优雅停机。"""
    code = asyncio.run(
        run_engine(
            str(config),
            host=host,
            port=port,
            shutdown_timeout=shutdown_timeout,
        )
    )
    raise typer.Exit(code)
