"""端到端测试 —— 优雅停机：停止后引擎转入非运行态，/health 报 down。"""

from __future__ import annotations

from httpx import AsyncClient

from wind_hub.assembly import AssembledRuntime, stop_runtime


async def test_graceful_shutdown_transitions_to_down(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
) -> None:
    """停机前运行中；显式 stop 后 running 翻转为 False 且 /health 报 down。"""
    before = await api_client.get("/health")
    assert before.json()["running"] is True
    assert before.json()["status"] == "ok"

    await stop_runtime(runtime)

    assert runtime.runtime.running is False

    after = await api_client.get("/health")
    assert after.status_code == 200
    assert after.json()["running"] is False
    assert after.json()["status"] == "down"


async def test_graceful_shutdown_is_idempotent(
    runtime: AssembledRuntime,
) -> None:
    """连续两次 stop 不抛异常（幂等）。"""
    await stop_runtime(runtime)
    await stop_runtime(runtime)
    assert runtime.runtime.running is False
