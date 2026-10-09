"""wind-hub-commander 独立进程入口。

Commander 独立运行：不依赖 Server，gRPC 入站适配器承载即时读写、诊断与
配置事务。默认启动后驻留等待停机信号；``--check`` 仅做配置加载校验。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .assembly import assemble_commander
from .infrastructure.grpc import build_grpc_server

logger = logging.getLogger(__name__)


def _install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """把 SIGINT/SIGTERM 映射为 Commander 优雅停机事件。"""
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
        loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    except NotImplementedError:
        logger.warning("当前事件循环不支持 add_signal_handler；请通过进程管理器停止 Commander")


async def run_commander(
    config_dir: str | Path,
    *,
    grpc_host: str = "127.0.0.1",
    grpc_port: int = 50052,
    shutdown_timeout: float = 30.0,
) -> int:
    """启动 Commander 与 gRPC Server，并阻塞到收到停机信号。"""
    app = assemble_commander(config_dir)
    grpc_server = build_grpc_server(app, host=grpc_host, port=grpc_port)
    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    try:
        await app.start()
        await grpc_server.start()
        logger.info(
            "wind-hub-commander 已启动 devices=%d config_hash=%s grpc=%s",
            len(app.runtime.devices),
            app.config_hash[:12],
            grpc_server.endpoint,
        )
        await shutdown_event.wait()
        logger.info("收到停机信号，Commander 开始优雅停机")
    finally:
        try:
            await grpc_server.stop()
        finally:
            try:
                # 与 Collector 同一语义：优雅停机超过硬超时即放弃等待
                # （退役会话的协议关闭挂起不能拖死进程退出）。
                await asyncio.wait_for(app.stop(), timeout=shutdown_timeout)
            except TimeoutError:
                logger.error(
                    "优雅停机超过 %.1fs 硬超时，强制退出（可能有资源未释放）",
                    shutdown_timeout,
                )

    logger.info("wind-hub-commander 已干净退出")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 Commander CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="wind-hub-commander",
        description="启动 Wind Hub Commander 即时设备操作进程。",
    )
    parser.add_argument("--config", required=True, help="现场配置目录")
    parser.add_argument("--grpc-host", default="127.0.0.1", help="gRPC 监听地址")
    parser.add_argument("--grpc-port", type=int, default=50052, help="gRPC 监听端口")
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
    """同步 Commander CLI 入口。"""
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args()
    if args.check:
        app = assemble_commander(args.config)
        logger.info(
            "配置校验通过 devices=%d disabled=%d config_hash=%s",
            len(app.runtime.devices),
            len(app.boot_config.disabled_devices),
            app.config_hash[:12],
        )
        return 0
    return asyncio.run(
        run_commander(
            args.config,
            grpc_host=args.grpc_host,
            grpc_port=args.grpc_port,
            shutdown_timeout=args.shutdown_timeout,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
