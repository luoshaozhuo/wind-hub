"""进程入口统一日志配置：UTC 时间戳 + 级别 + logger 名。"""

from __future__ import annotations

import logging
import time

_FORMAT = "%(asctime)sZ %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def configure_logging(level: int = logging.INFO) -> None:
    """配置根日志：时间戳一律 UTC（ISO-8601，秒级），便于跨进程对齐故障时序。"""
    formatter = logging.Formatter(fmt=_FORMAT, datefmt=_DATEFMT)
    formatter.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[handler], force=True)
