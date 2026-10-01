"""进程级 ADS 本机 AMS 身份初始化。

wind-hub 不自动向远端 PLC 写入 AMS route。现场路由由部署/诊断流程显式管理；
本模块只负责每进程一次的 open_port + set_local_address。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from wind_hub.config.schema import ADSSystemConfig

logger = logging.getLogger(__name__)


def _pyads() -> Any:
    """延迟导入可选 pyads。"""
    import pyads  # type: ignore[import-untyped]

    return pyads


_lock = asyncio.Lock()
_local_initialized = False
_local_config: ADSSystemConfig | None = None


async def ensure_local_initialized(config: ADSSystemConfig) -> None:
    """每进程只初始化一次本机 AMS 身份。"""
    global _local_initialized, _local_config
    async with _lock:
        if _local_initialized:
            return
        pyads = _pyads()
        await asyncio.to_thread(pyads.open_port)
        await asyncio.to_thread(pyads.set_local_address, config.local_ams_net_id)
        _local_config = config
        _local_initialized = True
        logger.info(
            "ADS: local AMS identity initialized (net id %s, ip %s)",
            config.local_ams_net_id,
            config.local_ip,
        )


def reset_for_tests() -> None:
    """重置进程级状态，仅供测试。"""
    global _local_initialized, _local_config
    _local_initialized = False
    _local_config = None
