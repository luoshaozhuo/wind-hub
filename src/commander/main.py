"""wind-hub-commander 独立进程入口。

Commander 独立运行：不依赖 Server，gRPC 入站适配器是可选组件（后续阶段
接入）。默认启动后驻留等待停机信号；``--check`` 仅做配置加载校验。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .assembly import assemble_commander

logger = logging.getLogger(__name__)


def _install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """把 SIGINT/SIGTERM 映射为 Commander 优雅停机事件。"""
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
        loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    except NotImplementedError:
        logger.warning("当前事件循环不支持 add_signal_handler；请通过进程管理器停止 Commander")


async def run_commander(config_dir: str | Path) -> int:
    """启动 Commander 并阻塞到收到停机信号。"""
    app = assemble_commander(config_dir)
    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    try:
        await app.start()
        logger.info(
            "wind-hub-commander 已启动 devices=%d config_hash=%s",
            len(app.runtime.devices),
            app.config_hash[:12],
        )
        await shutdown_event.wait()
        logger.info("收到停机信号，Commander 开始优雅停机")
    finally:
        await app.stop()

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
    return asyncio.run(run_commander(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
