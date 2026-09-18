"""端到端测试 —— 完整启动链路：装配 → 启动 → 服务真实化 → /health 返回 200。"""

from __future__ import annotations

from httpx import AsyncClient

from wind_hub.assembly import AssembledRuntime


async def test_full_startup_health_and_devices(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
) -> None:
    """启动后引擎处于运行态，/health 返回 200 且设备/服务已真实装配。"""
    # 引擎运行中
    assert runtime.scheduler.running is True

    # /health 返回 200（此前因 task_service 未真实化而 503）
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["running"] is True
    assert body["device_count"] == 2
    assert body["sink_count"] == 1


async def test_full_startup_device_listing(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
) -> None:
    """/devices 列出全部配置设备，单设备详情返回 protocol / connected。"""
    resp = await api_client.get("/devices")
    assert resp.status_code == 200
    devices = resp.json()
    ids = {d["device_id"] for d in devices}
    assert ids == {"modbus-1", "iec104-1"}

    detail = await api_client.get("/devices/modbus-1")
    assert detail.status_code == 200
    assert detail.json()["device_id"] == "modbus-1"
    assert detail.json()["protocol"] == "modbus"
    assert detail.json()["connected"] is True

    missing = await api_client.get("/devices/does-not-exist")
    assert missing.status_code == 404
