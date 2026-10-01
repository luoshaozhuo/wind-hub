"""wind-hub-collector 独立进程入口。

Collector 只负责装配并运行采集 Runtime、设备协议和 Sink；不启动 HTTP/Web API。
当前阶段仍从现场 YAML 配置目录装配 Runtime，后续由 Server 下发 resolved runtime config。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from wind_hub.application.runtime.collector_identity import (
    build_collector_identity,
    default_collector_id,
)
from wind_hub.assembly import assemble, start_runtime, stop_runtime

logger = logging.getLogger(__name__)


def _install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """将 SIGINT/SIGTERM 映射为 Collector 优雅停机事件。

    Args:
        shutdown_event: 进程停机事件。
    """
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)


async def run_collector(
    config_dir: str | Path,
    *,
    collector_id: str | None = None,
    shutdown_timeout: float = 30.0,
) -> int:
    """启动 Collector 并阻塞到收到停机信号。

    Args:
        config_dir: 当前过渡阶段使用的现场 YAML 配置目录。
        collector_id: Collector 稳定标识；为空时读取环境变量或主机名。
        shutdown_timeout: Runtime 优雅停机整体硬超时，单位秒。

    Returns:
        正常优雅停机返回 0。

    Notes:
        本入口不创建 FastAPI/uvicorn，也不注入 Admin AppContext。
        采集、协议、Task 和 Sink 的具体行为继续由既有 Runtime/assembly 实现。
    """
    logging.basicConfig(level=logging.INFO)

    identity = build_collector_identity(
        config_dir,
        collector_id=collector_id,
    )
    runtime = assemble(config_dir)
    runtime.log_store.install()
    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    try:
        await start_runtime(runtime)
        logger.info(
            (
                "wind-hub-collector 已启动 "
                "collector_id=%s boot_id=%s config_hash=%s "
                "（%d 台设备，%d 个 sink）"
            ),
            identity.collector_id,
            identity.boot_id,
            identity.config_hash,
            runtime.runtime.device_count,
            runtime.runtime.sink_count,
        )
        await shutdown_event.wait()
        logger.info("收到停机信号，开始优雅停机")
    finally:
        try:
            await stop_runtime(runtime, timeout=shutdown_timeout)
        finally:
            runtime.log_store.uninstall()

    logger.info("wind-hub-collector 已干净退出")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 Collector 进程参数解析器。

    Returns:
        独立的 argparse 参数解析器。
    """
    parser = argparse.ArgumentParser(
        prog="wind-hub-collector",
        description="启动 Wind Hub Collector 采集进程。",
    )
    parser.add_argument("--config", required=True, help="现场配置目录")
    parser.add_argument(
        "--collector-id",
        default=default_collector_id(),
        help=(
            "Collector 稳定标识；单机多 Collector 必须显式配置不同 ID，"
            "默认读取 WIND_HUB_COLLECTOR_ID 或主机名"
        ),
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=30.0,
        help="Runtime 优雅停机硬超时（秒）",
    )
    return parser


def main() -> int:
    """同步 CLI 入口。"""
    args = build_parser().parse_args()
    return asyncio.run(
        run_collector(
            args.config,
            collector_id=args.collector_id,
            shutdown_timeout=args.shutdown_timeout,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
