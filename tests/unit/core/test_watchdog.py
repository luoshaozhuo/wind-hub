"""Watchdog 周期健康检查与一次性重连契约。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.application import ConnectionHealth, WatchdogUseCase


class _Endpoint:
    def __init__(self) -> None:
        self.healthy = True
        self.health_error: BaseException | None = None
        self.health_gate: asyncio.Event | None = None
        self.connect_error: BaseException | None = None
        self.health_count = 0
        self.close_count = 0
        self.connect_count = 0
        self.callback_count = 0

    async def health(self) -> ConnectionHealth:
        self.health_count += 1
        if self.health_gate is not None:
            await self.health_gate.wait()
        if self.health_error is not None:
            raise self.health_error
        return ConnectionHealth(healthy=self.healthy)

    async def close(self) -> None:
        self.close_count += 1

    async def connect(self) -> None:
        self.connect_count += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.healthy = True

    async def callback(self) -> None:
        self.callback_count += 1


def _watchdog(endpoint: _Endpoint, **kwargs: Any) -> WatchdogUseCase:
    return WatchdogUseCase(
        health=endpoint.health,
        close=endpoint.close,
        connect=endpoint.connect,
        interval=float(kwargs.get("interval", 1.0)),
        health_timeout=float(kwargs.get("health_timeout", 0.1)),
        connect_timeout=float(kwargs.get("connect_timeout", 0.1)),
        on_success=endpoint.callback,
    )


@pytest.mark.asyncio
async def test_healthy_check_does_not_reconnect() -> None:
    endpoint = _Endpoint()
    result = await _watchdog(endpoint).check_once()
    assert result.healthy
    assert endpoint.close_count == endpoint.connect_count == 0


@pytest.mark.asyncio
async def test_unhealthy_check_reconnects_once_and_runs_callback() -> None:
    endpoint = _Endpoint()
    endpoint.healthy = False
    result = await _watchdog(endpoint).check_once()
    assert not result.healthy
    assert endpoint.close_count == 1
    assert endpoint.connect_count == 1
    assert endpoint.callback_count == 1


@pytest.mark.asyncio
async def test_health_exception_triggers_one_reconnect() -> None:
    endpoint = _Endpoint()
    endpoint.health_error = ConnectionError("lost")
    result = await _watchdog(endpoint).check_once()
    assert not result.healthy
    assert "lost" in (result.message or "")
    assert endpoint.connect_count == 1


@pytest.mark.asyncio
async def test_health_timeout_triggers_one_reconnect() -> None:
    endpoint = _Endpoint()
    endpoint.health_gate = asyncio.Event()
    watchdog = _watchdog(endpoint, health_timeout=0.01)
    result = await watchdog.check_once()
    assert not result.healthy
    assert endpoint.connect_count == 1


@pytest.mark.asyncio
async def test_connect_failure_does_not_run_callback() -> None:
    endpoint = _Endpoint()
    endpoint.healthy = False
    endpoint.connect_error = ConnectionError("still down")
    with pytest.raises(ConnectionError, match="still down"):
        await _watchdog(endpoint).check_once()
    assert endpoint.callback_count == 0
    assert endpoint.connect_count == 1


@pytest.mark.asyncio
async def test_callback_failure_is_observable_without_second_reconnect() -> None:
    endpoint = _Endpoint()
    endpoint.healthy = False

    async def bad_callback() -> None:
        endpoint.callback_count += 1
        raise RuntimeError("restore failed")

    watchdog = WatchdogUseCase(
        health=endpoint.health,
        close=endpoint.close,
        connect=endpoint.connect,
        interval=1.0,
        health_timeout=0.1,
        connect_timeout=0.1,
        on_success=bad_callback,
    )
    with pytest.raises(RuntimeError, match="restore failed"):
        await watchdog.check_once()
    assert endpoint.connect_count == 1
    assert endpoint.callback_count == 1


@pytest.mark.asyncio
async def test_explicit_reconnect_without_callback_is_supported() -> None:
    endpoint = _Endpoint()
    watchdog = WatchdogUseCase(
        health=endpoint.health,
        close=endpoint.close,
        connect=endpoint.connect,
        interval=1.0,
        health_timeout=0.1,
        connect_timeout=0.1,
    )
    await watchdog.reconnect()
    assert endpoint.close_count == endpoint.connect_count == 1


@pytest.mark.asyncio
async def test_concurrent_unhealthy_checks_do_not_duplicate_reconnect() -> None:
    endpoint = _Endpoint()
    endpoint.healthy = False
    watchdog = _watchdog(endpoint)
    await asyncio.gather(watchdog.check_once(), watchdog.check_once())
    assert endpoint.connect_count == 1
    assert endpoint.callback_count == 1


@pytest.mark.asyncio
async def test_next_cycle_can_check_and_reconnect_again() -> None:
    endpoint = _Endpoint()
    endpoint.healthy = False
    watchdog = _watchdog(endpoint)
    await watchdog.check_once()
    endpoint.healthy = False
    await watchdog.check_once()
    assert endpoint.connect_count == 2


@pytest.mark.asyncio
async def test_stop_prevents_future_reconnect() -> None:
    endpoint = _Endpoint()
    endpoint.healthy = False
    watchdog = _watchdog(endpoint, interval=30.0)
    await watchdog.start()
    await asyncio.sleep(0)
    await watchdog.stop()
    count = endpoint.connect_count
    await asyncio.sleep(0)
    assert endpoint.connect_count == count


@pytest.mark.asyncio
async def test_cancelled_health_propagates() -> None:
    endpoint = _Endpoint()
    endpoint.health_gate = asyncio.Event()
    watchdog = _watchdog(endpoint, health_timeout=30.0)
    task = asyncio.create_task(watchdog.check_once())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert endpoint.connect_count == 0


@pytest.mark.asyncio
async def test_stop_cancels_inflight_health_probe() -> None:
    endpoint = _Endpoint()
    endpoint.health_gate = asyncio.Event()
    watchdog = _watchdog(endpoint, interval=30.0, health_timeout=60.0)
    await watchdog.start()
    for _ in range(100):
        if endpoint.health_count:
            break
        await asyncio.sleep(0.001)
    assert endpoint.health_count == 1
    await asyncio.wait_for(watchdog.stop(), timeout=1.0)
    assert endpoint.connect_count == 0


@pytest.mark.asyncio
async def test_no_callback_when_stopped_during_reconnect() -> None:
    endpoint = _Endpoint()
    endpoint.healthy = False
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_connect() -> None:
        entered.set()
        await release.wait()
        endpoint.connect_count += 1

    endpoint.connect = slow_connect  # type: ignore[method-assign]
    watchdog = _watchdog(endpoint, connect_timeout=2.0)
    task = asyncio.create_task(watchdog.check_once())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    await watchdog.stop()
    release.set()
    await task
    assert endpoint.callback_count == 0


@pytest.mark.asyncio
async def test_repeated_start_and_stop_cycles_keep_single_task() -> None:
    """start 幂等、stop 后可重启；任意时刻至多一个周期任务。"""
    endpoint = _Endpoint()
    watchdog = _watchdog(endpoint, interval=30.0)
    await watchdog.start()
    first_task = watchdog._task
    await watchdog.start()
    assert watchdog._task is first_task

    await watchdog.stop()
    assert first_task is not None and first_task.done()

    await watchdog.start()
    assert watchdog._task is not first_task
    for _ in range(100):
        if endpoint.health_count:
            break
        await asyncio.sleep(0.001)
    assert endpoint.health_count >= 1
    await watchdog.stop()
