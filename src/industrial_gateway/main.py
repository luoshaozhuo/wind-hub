"""Industrial Gateway 独立进程入口（无需修改仓库现有模块）。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .assembly import assemble_gateway
from .infrastructure.config.loader import load_config

logger = logging.getLogger(__name__)


async def run(config_path: str | Path) -> None:
    config = load_config(config_path)
    runtime = assemble_gateway(config)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, shutdown.set)
        except NotImplementedError:
            logger.warning("signal handlers not supported: %s", signum)
    try:
        await runtime.start()
        logger.info("industrial gateway started: %d endpoints", len(config.devices))
        await shutdown.wait()
    finally:
        await runtime.stop()


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m industrial_gateway.main")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    if args.check:
        logger.info("configuration valid: %d endpoints", len(config.devices))
        return 0
    asyncio.run(run(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
