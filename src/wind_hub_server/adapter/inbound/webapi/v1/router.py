"""Admin API v1 路由组合入口。

Admin API v1 当前覆盖 Phase 1–5：运行总览、设备/任务、Data/Trend/Command、
Config/Settings/Definitions、Sinks/Diagnostics、Quality/Logs/System Health。
路由按能力拆分到同包模块；这里只负责挂载，不定义任何端点。
"""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub_server.adapter.inbound.webapi.v1 import (
    config,
    devices,
    diagnostics,
    monitoring,
    operations,
    sinks,
    tasks,
    workers,
)

router = APIRouter(prefix="/api/v1")
router.include_router(monitoring.router)
router.include_router(workers.router)
router.include_router(devices.router)
router.include_router(tasks.router)
router.include_router(operations.router)
router.include_router(config.router)
router.include_router(sinks.router)
router.include_router(diagnostics.router)
