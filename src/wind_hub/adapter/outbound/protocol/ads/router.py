"""进程级 ADS 本机路由器初始化与 route 一次性修复。

Linux / WSL mirrored / 容器环境下，pyads 以本机作为 AMS 路由器通信，需要：

1. 进程启动时执行一次 ``pyads.open_port()`` +
   ``pyads.set_local_address(local_ams_net_id)`` —— 本机 AMS 身份是**进程级**
   的，配置来自 ``system.yaml`` 的 ``ads`` 段（
   :class:`~wind_hub.config.schema.ADSSystemConfig`），由 assembly 的
   ``start_runtime`` 在存在 ADS 设备时调用 :func:`ensure_local_initialized`
   完成；任何 :class:`ADSDriver` 都不会重复执行。
2. 首次连接某台 PLC 失败时，若 ``route_repair.enabled``，执行**一次**
   ``pyads.add_route_to_plc`` 注册本机 route 后重连（
   :func:`repair_route_once`）。每台 PLC 在一个进程生命周期内最多修复一次，
   失败不反复执行。

pyads 暴露在外的全局关闭入口只有 ``close_port``，它会关掉进程内所有 ADS
端口；当前架构下进程退出即释放，故不提供对称关闭（避免过度设计）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from wind_hub.config.schema import ADSSystemConfig

logger = logging.getLogger(__name__)


def _pyads() -> Any:
    """Return the ``pyads`` module, importing it lazily (untyped, optional extra)."""
    import pyads  # type: ignore[import-untyped]

    return pyads


_lock = asyncio.Lock()
_local_initialized = False
_local_config: ADSSystemConfig | None = None
#: 本进程生命周期内已尝试修复过 route 的 PLC（按 IP）；失败也记入，防止反复修复。
_repair_attempted_plcs: set[str] = set()


async def ensure_local_initialized(config: ADSSystemConfig) -> None:
    """Initialize the process-level local AMS identity exactly once.

    Subsequent calls (including concurrent ones) are no-ops.  pyads calls are
    synchronous and run on a worker thread.
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


async def repair_route_once(plc_ip: str) -> bool:
    """Attempt ``add_route_to_plc`` for *plc_ip* at most once per process.

    Returns:
        ``True`` when a route was (re)registered and a reconnect is worth
        trying; ``False`` when repair is disabled/not initialized, already
        attempted for this PLC, or the repair call itself failed.
    """
    global _repair_attempted_plcs
    if not _local_initialized or _local_config is None:
        return False
    repair = _local_config.route_repair
    if not repair.enabled:
        return False
    async with _lock:
        if plc_ip in _repair_attempted_plcs:
            return False
        _repair_attempted_plcs.add(plc_ip)
    pyads = _pyads()
    try:
        await asyncio.to_thread(
            pyads.add_route_to_plc,
            _local_config.local_ams_net_id,
            _local_config.local_ip,
            plc_ip,
            username=repair.username,
            password=repair.password,
            route_name=repair.route_name,
        )
    except Exception as exc:
        # 修复失败不反复执行（IP 已记入 attempted 集合）；返回 False 让调用方
        # 进入正常 reconnect 机制。
        logger.warning("ADS: route repair for %s failed: %s", plc_ip, exc)
        return False
    logger.info(
        "ADS: route '%s' registered on PLC %s (local %s / %s)",
        repair.route_name,
        plc_ip,
        _local_config.local_ams_net_id,
        _local_config.local_ip,
    )
    return True


def reset_for_tests() -> None:
    """Reset process-level state — test-only helper."""
    global _local_initialized, _local_config, _repair_attempted_plcs
    _local_initialized = False
    _local_config = None
    _repair_attempted_plcs = set()
