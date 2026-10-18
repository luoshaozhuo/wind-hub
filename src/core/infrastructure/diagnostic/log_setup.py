"""进程入口统一日志配置：UTC 时间戳 + 级别 + logger 名。"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from core.application.settings import LogSettings

_FORMAT = "%(asctime)sZ %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def configure_logging(level: int = logging.INFO) -> None:
    """配置根日志：时间戳一律 UTC（ISO-8601，秒级），便于跨进程对齐故障时序。"""
    formatter = logging.Formatter(fmt=_FORMAT, datefmt=_DATEFMT)
    formatter.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[handler], force=True)


def configure_process_logging(config: LogSettings, *, process_name: str) -> None:
    """按配置设置进程根日志；日志文件与过期清理由进程独立拥有。"""
    if not process_name or not process_name.replace("-", "_").replace("_", "").isalnum():
        raise ValueError("invalid process_name")
    # 后续进程接线前仅提供独立入口，不改变既有 CLI 的控制台日志。
    configure_logging(getattr(logging, config.level))
