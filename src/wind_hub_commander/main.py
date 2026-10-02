"""wind-hub-commander 独立进程入口。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from wind_hub_commander.adapter.inbound.grpc import build_grpc_server
from wind_hub_commander.assembly import assemble_commander

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
) -> int:
    """启动 Commander 并阻塞到收到停机信号。"""
    logging.basicConfig(level=logging.INFO)

    app = assemble_commander(config_dir)
    grpc_server = build_grpc_server(app, host=grpc_host, port=grpc_port)
    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    try:
        await app.runtime.start()
        await grpc_server.start()
        logger.info(
            "wind-hub-commander 已启动 endpoint=%s devices=%d",
            grpc_server.endpoint,
            len(app.runtime.devices),
        )
        await shutdown_event.wait()
        logger.info("收到停机信号，Commander 开始优雅停机")
    finally:
        try:
            await grpc_server.stop()
        finally:
            await app.runtime.stop()

    logger.info("wind-hub-commander 已干净退出")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 Commander CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="wind-hub-commander",
        description="启动 Wind Hub Commander 即时设备操作进程。",
    )
    parser.add_argument("--config", required=True, help="现场配置目录")
    parser.add_argument(
        "--grpc-host",
        default="127.0.0.1",
        help="gRPC 监听地址，默认 127.0.0.1",
    )
    parser.add_argument(
        "--grpc-port",
        type=int,
        default=50052,
        help="gRPC 监听端口，默认 50052",
    )
    return parser


def main() -> int:
    """同步 Commander CLI 入口。"""
    args = build_parser().parse_args()
    return asyncio.run(
        run_commander(
            args.config,
            grpc_host=args.grpc_host,
            grpc_port=args.grpc_port,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
