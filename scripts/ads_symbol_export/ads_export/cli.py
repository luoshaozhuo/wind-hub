"""Linux 独立脚本入口。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config
from .connection import _inspect_environment, _open_connection, _probe_runtime
from .strategies import StrategyOutcome, export_symbols

_LOG = logging.getLogger(__name__)


def main() -> int:
    """先校验配置，再连接 PLC 并导出。"""
    parser = argparse.ArgumentParser(description="TwinCAT ADS 符号表导出")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    settings = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    env = _inspect_environment(settings) if settings.CHECK_HOST_ENVIRONMENT else None
    destination = Path.cwd() / settings.OUTPUT_FILENAME
    plc = _open_connection(settings, env)
    outcomes: list[StrategyOutcome] = []
    _LOG.info("PLC 动态信息：%s", _probe_runtime(plc, settings))
    total = export_symbols(
        plc, destination, settings=settings,
        reconnect=lambda: _open_connection(settings, env),
        outcomes=outcomes, owns_connection=True,
    )
    for outcome in outcomes:
        _LOG.info("%s", outcome.describe())
    _LOG.info("已写入 %d 个变量名: %s", total, destination)
    return 0
