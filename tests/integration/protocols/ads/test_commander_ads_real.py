"""Commander × 真实 ADS 协议路径集成测试。

服务端由 ``ads_service`` fixture 提供（见同级 ``conftest.py``）：

- 配置 ``WIND_HUB_TEST_ADS_HOST`` / ``WIND_HUB_TEST_ADS_NET_ID`` 等环境变量时
  直连真实 TwinCAT PLC（见 ``tests/test.env.example``），此时模块附加
  ``hardware`` marker，由 ci-hardware.yml 经 ``-m hardware`` 选择；
- 未配置时在进程内拉起 pyads ``AdsTestServer``（真实 AMS/TCP 协议，
  ``real_service``），默认 CI 全量执行。

两条路径都不降级为 mock；testserver 路径的读/写符号由 fixture 固定提供，
真实 PLC 路径未配置读/写符号时对应用例单独 SKIPPED。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.support.config_helper import write_config_tree
from tests.support.env import ads_config_from_env
from wind_hub_commander.assembly import CommanderApp, assemble_commander
from wind_hub_core.model.command import Command

#: ``hardware`` 仅在当前环境配置了真实 PLC 时附加——该 marker 按仓库规则
#: 只用于真实硬件；testserver 路径只声明 real_service。
pytestmark = [pytest.mark.real_service]
if ads_config_from_env() is not None:
    pytestmark.append(pytest.mark.hardware)


def _write_ads_config(base: Path, ads: dict[str, str | int]) -> Path:
    points = []
    if ads["read_symbol"]:
        points.append(
            {
                "point_id": "plc.readback",
                "point_groups": ["telemetry"],
                "address": {"symbol": ads["read_symbol"]},
                "data_type": "float32",
            }
        )
    if ads["write_symbol"]:
        points.append(
            {
                "point_id": "plc.setpoint",
                "point_groups": ["telemetry", "control"],
                "address": {"symbol": ads["write_symbol"]},
                "data_type": "float32",
            }
        )
    system: dict = {"runtime": {"connect_timeout": 10.0, "read_timeout": 5.0}}
    if ads["local_net_id"] and ads["local_ip"]:
        system["ads"] = {
            "local_ams_net_id": ads["local_net_id"],
            "local_ip": ads["local_ip"],
        }
    return write_config_tree(
        base,
        devices=[
            {
                "device_id": "ads-1",
                "protocol": "ads",
                "point_table": "ads",
                "endpoint": {
                    "host": ads["host"],
                    "port": ads["port"],
                    "extensions": {
                        "target_net_id": ads["net_id"],
                        "target_port": ads["port"],
                        "timeout": 5.0,
                    },
                },
            }
        ],
        point_tables={"ads": {"points": points}},
        sinks=[],
        tasks=[],
        system=system,
    )


@pytest.fixture
async def ads_runtime(
    tmp_path: Path,
    ads_service: dict[str, str | int],
) -> AsyncIterator[CommanderApp]:
    config_dir = _write_ads_config(tmp_path / "cfg", ads_service)
    app = assemble_commander(config_dir)
    await app.runtime.start()
    try:
        yield app
    finally:
        await app.runtime.stop()


class TestRealAdsConnection:
    async def test_device_connects_to_ads_server(self, ads_runtime) -> None:
        result = await ads_runtime.diagnostic.verify_device("ads-1", timeout=3.0)
        stage = next(s for s in result.stages if s.name == "protocol")
        assert stage.ok, stage.message


class TestRealAdsRead:
    async def test_read_symbol(
        self,
        ads_runtime,
        ads_service: dict[str, str | int],
    ) -> None:
        if not ads_service["read_symbol"]:
            pytest.skip("SKIPPED: WIND_HUB_TEST_ADS_READ_SYMBOL not configured")
        value = await ads_runtime.read.read_point("ads-1", "plc.readback")
        assert value.value is not None

    async def test_verify_point_resolves_symbol_address(
        self,
        ads_runtime,
        ads_service: dict[str, str | int],
    ) -> None:
        if not ads_service["read_symbol"]:
            pytest.skip("SKIPPED: WIND_HUB_TEST_ADS_READ_SYMBOL not configured")
        result = await ads_runtime.diagnostic.verify_point("ads-1", "plc.readback")
        assert result.resolved_address is not None
        assert result.resolved_address["index_group"] is not None
        assert result.resolved_address["index_offset"] is not None
        assert result.ok, result.error


class TestRealAdsWrite:
    async def test_write_symbol(
        self,
        ads_runtime,
        ads_service: dict[str, str | int],
    ) -> None:
        if not ads_service["write_symbol"]:
            pytest.skip("SKIPPED: WIND_HUB_TEST_ADS_WRITE_SYMBOL not configured")
        result = await ads_runtime.command.send(
            Command(
                command_id="ads-real-write-1",
                device_id="ads-1",
                point_id="plc.setpoint",
                value=1.0,
            )
        )
        assert result.success, result.error
