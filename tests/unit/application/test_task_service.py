"""Unit tests for the TaskService application service.

验证对象：``application/task_service.py`` 把生命周期控制委托给 Scheduler，
并把配置热重载委托给 ConfigService（失败折叠为 ``ConfigError``）；
``status()`` 从 scheduler 聚合出真实的 ``SystemStatus`` 快照。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.application.config_service import ConfigService
from wind_hub.application.task_service import TaskService
from wind_hub.domain.engine.scheduler import Scheduler
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.model.reload import ConfigDiff, ReloadResult
from wind_hub.domain.port.outbound import HealthStatus


def _scheduler() -> MagicMock:
    sched = MagicMock(spec=Scheduler)
    sched.start = AsyncMock()
    sched.stop = AsyncMock()
    return sched


def _config_service(success: bool = True, errors: list[str] | None = None) -> MagicMock:
    cfg = MagicMock(spec=ConfigService)
    cfg.reload = AsyncMock(
        return_value=ReloadResult(success=success, diff=ConfigDiff(), errors=errors or [])
    )
    return cfg


# ---------------------------------------------------------------------------
# lifecycle delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_delegates_to_scheduler() -> None:
    sched = _scheduler()
    await TaskService(sched).start()
    sched.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_delegates_to_scheduler() -> None:
    sched = _scheduler()
    await TaskService(sched).stop()
    sched.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# reload_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_config_success() -> None:
    cfg = _config_service(success=True)
    await TaskService(_scheduler(), cfg).reload_config()
    cfg.reload.assert_awaited_once()


@pytest.mark.asyncio
async def test_reload_config_failure_raises_config_error() -> None:
    cfg = _config_service(success=False, errors=["bad yaml"])
    with pytest.raises(ConfigError, match="bad yaml"):
        await TaskService(_scheduler(), cfg).reload_config()


@pytest.mark.asyncio
async def test_reload_config_without_config_service_raises() -> None:
    with pytest.raises(ConfigError, match="not wired"):
        await TaskService(_scheduler()).reload_config()


# ---------------------------------------------------------------------------
# status() aggregation
# ---------------------------------------------------------------------------


def _healthy(flag: bool) -> HealthStatus:
    return HealthStatus(healthy=flag)


@pytest.mark.asyncio
async def test_status_aggregates_running_and_health() -> None:
    sched = _scheduler()
    # Scheduler.running / device_count / sink_count are properties; health()
    # returns devices first, then sinks (the order TaskService relies on).
    sched.running = True
    sched.device_count = 2
    sched.sink_count = 2
    sched.points_collected = 120
    sched.points_routed = 118
    sched.points_dropped = 2
    sched.health.return_value = {
        "modbus-1": _healthy(True),
        "iec104-1": _healthy(False),
        "null_sink": _healthy(True),
        "other_sink": _healthy(True),
    }

    status = await TaskService(sched).status()

    assert status.running is True
    assert status.device_count == 2
    assert status.sink_count == 2
    # 2 devices: 1 healthy → devices_connected == 1
    assert status.devices_connected == 1
    # 2 sinks: both healthy → sinks_healthy == 2
    assert status.sinks_healthy == 2
    # 决策 7：调度器统计原样透传
    assert status.points_collected == 120
    assert status.points_routed == 118
    assert status.points_dropped == 2


@pytest.mark.asyncio
async def test_status_reports_stopped() -> None:
    sched = _scheduler()
    sched.running = False
    sched.device_count = 0
    sched.sink_count = 0
    sched.points_collected = 0
    sched.points_routed = 0
    sched.points_dropped = 0
    sched.health.return_value = {}

    status = await TaskService(sched).status()

    assert status.running is False
    assert status.device_count == 0
    assert status.sink_count == 0
    assert status.devices_connected == 0
    assert status.sinks_healthy == 0
    assert status.points_collected == 0
    assert status.points_routed == 0
    assert status.points_dropped == 0
