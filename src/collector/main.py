"""wind-hub-collector 独立进程入口。

Collector 独立运行：不依赖 Commander，gRPC 控制面承载低频状态查询、Task
控制、Sink 检查与配置事务。默认启动后驻留等待停机信号；``--check`` 仅做
配置加载校验。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .assembly import assemble_collector
from .infrastructure.grpc import build_grpc_server

logger = logging.getLogger(__name__)


def _install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """把 SIGINT/SIGTERM 映射为 Collector 优雅停机事件。"""
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
        loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    except NotImplementedError:
        logger.warning("当前事件循环不支持 add_signal_handler；请通过进程管理器停止 Collector")


async def run_collector(
    config_dir: str | Path,
    *,
    collector_id: str | None = None,
    grpc_host: str = "127.0.0.1",
    grpc_port: int = 50051,
    shutdown_timeout: float = 30.0,
) -> int:
    """启动 Collector 与 gRPC 控制面，并阻塞到收到停机信号。

    Args:
        config_dir: 现场配置目录。
        collector_id: Collector 稳定标识；为空时读取环境变量或主机名。
        grpc_host: gRPC 控制面监听地址。
        grpc_port: gRPC 控制面监听端口。
        shutdown_timeout: 优雅停机整体硬超时（秒）。超时后记录日志并
            强制退出进程，避免单个卡死的资源释放阻断停机（与旧
            Collector ``--shutdown-timeout`` 语义一致）。
    """
    app = assemble_collector(config_dir, collector_id=collector_id)
    grpc_server = build_grpc_server(app, app.identity, host=grpc_host, port=grpc_port)
    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    try:
        await app.start()
        await grpc_server.start()
        logger.info(
            "wind-hub-collector 已启动 devices=%d sinks=%d tasks=%d "
            "collector_id=%s boot_id=%s config_hash=%s grpc=%s",
            len(app.boot_config.devices),
            len(app.boot_config.sinks),
            len(app.boot_config.tasks),
            app.identity.collector_id,
            app.identity.boot_id,
            app.config_hash[:12],
            grpc_server.endpoint,
        )
        await shutdown_event.wait()
        logger.info("收到停机信号，Collector 开始优雅停机")
    finally:
        try:
            await grpc_server.stop()
        finally:
            try:
                await asyncio.wait_for(app.stop(), timeout=shutdown_timeout)
            except TimeoutError:
                logger.error(
                    "优雅停机超过 %.1fs 硬超时，强制退出（可能有资源未释放）",
                    shutdown_timeout,
                )

    logger.info("wind-hub-collector 已干净退出")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 Collector CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="wind-hub-collector",
        description="启动 Wind Hub Collector 采集进程。",
    )
    parser.add_argument("--config", required=True, help="现场配置目录")
    parser.add_argument(
        "--collector-id",
        default=None,
        help="Collector 稳定标识（默认读取 WIND_HUB_COLLECTOR_ID 或主机名）",
    )
    parser.add_argument("--grpc-host", default="127.0.0.1", help="gRPC 控制面监听地址")
    parser.add_argument("--grpc-port", type=int, default=50051, help="gRPC 控制面监听端口")
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=30.0,
        help="优雅停机整体硬超时（秒，默认 30）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅加载并校验配置后退出，不启动运行时",
    )
    return parser


def main() -> int:
    """同步 Collector CLI 入口。"""
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args()
    if args.check:
        app = assemble_collector(args.config, collector_id=args.collector_id)
        logger.info(
            "配置校验通过 devices=%d disabled=%d sinks=%d tasks=%d config_hash=%s",
            len(app.boot_config.devices),
            len(app.boot_config.disabled_devices),
            len(app.boot_config.sinks),
            len(app.boot_config.tasks),
            app.config_hash[:12],
        )
        return 0
    return asyncio.run(
        run_collector(
            args.config,
            collector_id=args.collector_id,
            grpc_host=args.grpc_host,
            grpc_port=args.grpc_port,
            shutdown_timeout=args.shutdown_timeout,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
