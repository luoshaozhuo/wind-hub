"""进程级 ADS 本机 AMS 身份初始化。

wind-hub 不自动向远端 PLC 写入 AMS route。现场路由由部署/诊断流程显式管理；
本模块只负责每进程一次的 open_port + set_local_address。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from wind_hub_core.config import ADSSystemConfig

logger = logging.getLogger(__name__)


def _pyads() -> Any:
    """延迟导入可选 pyads，并把未类型化第三方模块限制在本适配边界。"""
    # pyads 尚未提供稳定类型声明；待上游 typing 可用后移除抑制。
    import pyads  # type: ignore[import-untyped]

    return pyads


_lock = asyncio.Lock()
_local_initialized = False
_local_config: ADSSystemConfig | None = None


async def ensure_local_initialized(config: ADSSystemConfig) -> None:
    """每进程只初始化一次本机 AMS 身份。

    Args:
        config: 系统级 ADS 本机地址配置。

    Notes:
        本函数只调用本机 ADS API，不向远端 PLC 新增或修改 AMS route；重复调用
        在同一进程内直接返回，以避免重复 open_port/set_local_address。
    """
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
