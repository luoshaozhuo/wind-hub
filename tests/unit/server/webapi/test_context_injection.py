"""AppContext 显式注入的架构约束测试。

守护目标：Web 请求路径不经过进程级 service locator——每个 FastAPI app
实例只看到自己构造时注入的 context，不同实例之间完全隔离。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from wind_hub_server.adapter.inbound.webapi.app import build_api
from wind_hub_server.application.app_context import AppContext
from wind_hub_server.application.monitoring.overview import OverviewSnapshot


def _overview_context(site_id: str) -> AppContext:
    overview = MagicMock()
    overview.snapshot.return_value = OverviewSnapshot(
        site_id=site_id,
        site_name=site_id,
        runtime_running=True,
        runtime_state="running",
        workers_unavailable=[],
        device_count=0,
        devices_connected=0,
        devices_offline=0,
        sink_count=0,
        sinks_healthy=0,
        task_count=0,
        task_instances=0,
        task_instances_running=0,
        task_instances_failed=0,
        points_collected=0,
        points_routed=0,
        points_dropped=0,
    )
    return AppContext(overview=overview)


def test_each_app_uses_only_its_injected_context() -> None:
    """并行存在的两个 app 实例互不串 context——不存在共享全局状态。"""
    client_a = TestClient(build_api(_overview_context("site-a")))
    client_b = TestClient(build_api(_overview_context("site-b")))

    assert client_a.get("/api/v1/overview").json()["site_id"] == "site-a"
    assert client_b.get("/api/v1/overview").json()["site_id"] == "site-b"
    # 交错请求仍然各自隔离。
    assert client_a.get("/api/v1/overview").json()["site_id"] == "site-a"


def test_context_lives_on_app_state() -> None:
    """context 的唯一存放点是 app.state——adapter 内没有模块级保存。"""
    ctx = _overview_context("state")
    app = build_api(ctx)
    assert app.state.app_context is ctx
