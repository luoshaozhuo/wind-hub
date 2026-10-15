"""共用 scripts/protocol_servers.py 的真实三协议×十站端到端测试。

配置生成与 Web Server E2E 使用同一 Core 文件结构。所有协议地址显式
映射到不同 loopback 端点；不需要真实 PLC，也没有伪造网络响应。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scripts.protocol_servers import (
    EndpointPlan,
    plan_devices,
    run_fleet,
    validate_points,
)
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import free_port
from tests.system.test_web_fleet_config_e2e import build_fleet_config
from tests.system.test_web_protocol_fleet_e2e import ProtocolFleet
from web_server.application.config.service import ConfigService

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.asyncio]


@pytest.fixture
def virtual_site(tmp_path: Path) -> Path:
    """纯地址规划，不预先 bind；实际 listener 必须由业务脚本启动。"""
    modbus = [ModbusMockServer(port=free_port()) for _ in range(10)]
    iec104 = [IEC104MockServer(port=free_port()) for _ in range(10)]
    ads_hosts = [f"127.0.1.{index + 11}" for index in range(10)]
    return build_fleet_config(
        tmp_path / "site",
        ProtocolFleet(modbus=modbus, iec104=iec104, ads_hosts=ads_hosts),
    )


def test_script_follows_exact_core_snapshot_and_point_definitions(
    virtual_site: Path,
) -> None:
    snapshot = ConfigService.load_directory(virtual_site)
    plans = plan_devices(snapshot)
    assert len(plans) == 30
    assert {family: sum(p.protocol == family for p in plans)
            for family in ("modbus", "ads", "iec104")} == {
        "modbus": 10,
        "ads": 10,
        "iec104": 10,
    }
    assert len({plan.address for plan in plans}) == 30
    for plan in plans:
        assert plan.points
        validate_points(plan)
        assert {point.point_id for point in plan.points} == set(plan.data_types)


async def test_script_runs_all_thirty_real_servers_with_random_cycles(
    virtual_site: Path,
) -> None:
    async with run_fleet(virtual_site, interval=0.2, seed=5) as plans:
        assert len(plans) == 30

        async def open_endpoint(plan: EndpointPlan) -> None:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(plan.host, plan.port),
                timeout=3,
            )
            del reader
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=3)

        await asyncio.gather(*(open_endpoint(plan) for plan in plans))
        await asyncio.sleep(0.65)
        await asyncio.gather(*(open_endpoint(plan) for plan in plans))


async def test_script_restart_releases_all_thirty_ports(virtual_site: Path) -> None:
    for _ in range(2):
        async with run_fleet(virtual_site, interval=0.25, seed=8) as plans:
            assert len(plans) == 30
            await asyncio.sleep(0.3)


@pytest.mark.parametrize("interval", [-1, 0, 0.001, 3601])
async def test_script_rejects_invalid_sampling_interval(
    virtual_site: Path, interval: float
) -> None:
    with pytest.raises(ValueError, match="interval"):
        async with run_fleet(virtual_site, interval=interval):
            pytest.fail("invalid interval unexpectedly created sockets")


def test_configured_bind_must_be_loopback_by_default(virtual_site: Path) -> None:
    snapshot = ConfigService.load_directory(virtual_site)
    devices = snapshot.site.devices
    assert len(devices) == 30
    assert all(
        plan.host.startswith("127.") for plan in plan_devices(snapshot)
    )


async def test_port_collision_exits_without_hanging(virtual_site: Path) -> None:
    plans = plan_devices(ConfigService.load_directory(virtual_site))
    first = next(plan for plan in plans if plan.protocol == "modbus")
    server = await asyncio.start_server(lambda r, w: w.close(), first.host, first.port)
    try:
        with pytest.raises(OSError):
            async with run_fleet(virtual_site, interval=0.1):
                pytest.fail("bind collision was ignored")
    finally:
        server.close()
        await server.wait_closed()
