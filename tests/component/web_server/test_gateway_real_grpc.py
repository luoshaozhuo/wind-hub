"""Web Server → 真实 Collector/Commander gRPC 控制面的组件测试。

两端真实装配（assemble_collector / assemble_commander）+ 真实 gRPC
Server，Web 层用真实 RpcGateway 与 ASGI transport——不经 mock、不经
TestClient 的跨事件循环语义。验证 REST↔gRPC 网关的 wire 契约与错误
透传（上游 UNAVAILABLE → 503，不得伪装成空结果）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from collector.assembly import assemble_collector
from collector.infrastructure.grpc import build_grpc_server as build_collector_server
from commander.assembly import assemble_commander
from commander.infrastructure.grpc import build_grpc_server as build_commander_server
from tests.support.new_collector import write_collector_config_tree
from tests.support.new_commander import write_minimal_config_tree
from web_server.app import create_app, get_gateway
from web_server.rpc import RpcGateway, RpcSettings


@pytest.fixture
async def stack(tmp_path) -> AsyncIterator[tuple[httpx.AsyncClient, RpcSettings]]:
    """启动真实 Collector + Commander gRPC 与挂真网关的 Web 应用。"""
    offline_device = {
        "device_id": "dev1",
        "model": "mod",
        "endpoint": {"host": "127.0.0.1", "port": 1},  # 连接立即拒绝
    }
    collector_app = assemble_collector(
        write_collector_config_tree(
            tmp_path / "collector",
            devices=[offline_device],
            runtime={"connect_timeout": 0.5},
        ),
        collector_id="collector-gw-test",
    )
    commander_app = assemble_commander(
        write_minimal_config_tree(
            tmp_path / "commander",
            devices=[offline_device],
            runtime={"connect_timeout": 0.5, "read_timeout": 0.5},
        )
    )
    collector_server = build_collector_server(
        collector_app, collector_app.identity, host="127.0.0.1", port=0
    )
    commander_server = build_commander_server(commander_app, host="127.0.0.1", port=0)
    await collector_app.start()
    await commander_app.start()
    await collector_server.start()
    await commander_server.start()

    settings = RpcSettings(
        collector_target=collector_server.endpoint,
        commander_target=commander_server.endpoint,
        timeout_seconds=2.0,
    )
    app = create_app(settings)
    async with RpcGateway(settings) as gateway:
        app.dependency_overrides[get_gateway] = lambda: gateway
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw.test"
        ) as client:
            yield client, settings

    await collector_server.stop(0)
    await commander_server.stop(0)
    await collector_app.stop()
    await commander_app.stop()


async def test_collector_info_over_real_grpc(stack) -> None:
    client, _ = stack
    response = await client.get("/api/v1/collector/info")
    assert response.status_code == 200
    body = response.json()
    assert body["component"] == "wind-hub-collector"
    assert body["collector_id"] == "collector-gw-test"
    assert body["active_config_hash"]


async def test_collector_tasks_and_devices_over_real_grpc(stack) -> None:
    client, _ = stack
    tasks = await client.get("/api/v1/collector/tasks")
    assert tasks.status_code == 200
    assert isinstance(tasks.json()["items"], list)

    devices = await client.get("/api/v1/collector/devices")
    assert devices.status_code == 200
    items = devices.json()["items"]
    assert [d["device_id"] for d in items] == ["dev1"]
    # proto3 JSON 省略默认值：离线设备 connected=false 不出现在载荷里。
    assert items[0].get("connected", False) is False  # 离线设备如实上报


async def test_commander_devices_over_real_grpc(stack) -> None:
    client, _ = stack
    response = await client.get("/api/v1/commander/devices")
    assert response.status_code == 200
    devices = response.json()["devices"]
    assert [d["device_id"] for d in devices] == ["dev1"]
    assert devices[0].get("healthy", False) is False


async def test_commander_read_offline_device_maps_precondition(stack) -> None:
    """离线设备读取：Commander FAILED_PRECONDITION → HTTP 409（不伪装 200）。"""
    client, _ = stack
    response = await client.get("/api/v1/commander/devices/dev1/points/p1")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "FAILED_PRECONDITION"


async def test_unknown_device_maps_not_found(stack) -> None:
    client, _ = stack
    response = await client.get("/api/v1/commander/devices/ghost/points/p1")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


async def test_upstream_down_maps_503_not_empty(stack) -> None:
    """上游不可达：503 + UNAVAILABLE，绝不伪装成空列表。"""
    _, settings = stack  # stack 已停？未停——这里新建一个指向死端口的网关
    dead = RpcSettings(
        collector_target="127.0.0.1:1",
        commander_target=settings.commander_target,
        timeout_seconds=0.5,
    )
    app = create_app(dead)
    async with RpcGateway(dead) as gateway:
        app.dependency_overrides[get_gateway] = lambda: gateway
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as client:
            response = await client.get("/api/v1/collector/tasks")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "UNAVAILABLE"
