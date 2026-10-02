"""ADS 驱动 × 真实 TwinCAT PLC 集成测试（环境驱动）。

本模块**只在**配置了真实 ADS 环境时执行（见 ``tests/test.env.example``）：

- 必填：``WIND_HUB_TEST_ADS_HOST`` / ``WIND_HUB_TEST_ADS_NET_ID``
- 可选：``WIND_HUB_TEST_ADS_PORT``（默认 801）、
  ``WIND_HUB_TEST_ADS_READ_SYMBOL`` / ``WIND_HUB_TEST_ADS_WRITE_SYMBOL``、
  本机 AMS 身份 ``WIND_HUB_TEST_ADS_LOCAL_NET_ID`` / ``..._LOCAL_IP``

未配置时整模块 SKIPPED——**绝不**降级为 mock 冒充真实验收。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.component.collector.conftest import functional_sink_factory
from tests.support.config_helper import write_config_tree
from tests.support.env import ads_config_from_env
from wind_hub_collector.assembly import assemble, start_runtime, stop_runtime
from wind_hub_core.model.command import Command

ADS = ads_config_from_env()
if ADS is None:
    pytest.skip(
        "SKIPPED: real ADS environment not configured",
        allow_module_level=True,
    )

#: 真实 TwinCAT/PLC 验收：协议、服务真实性与环境属性全部显式标注——
#: ci-hardware.yml 经 ``-m hardware`` 选择本模块（环境未配置时整模块
#: SKIPPED，见文件头说明）。
pytestmark = [
    pytest.mark.ads,
    pytest.mark.hardware,
    pytest.mark.real_service,
]


def _write_ads_config(base: Path) -> Path:
    points = []
    if ADS["read_symbol"]:
        points.append(
            {
                "point_id": "plc.readback",
                "point_groups": ["telemetry"],
                "address": {"symbol": ADS["read_symbol"]},
                "data_type": "float32",
            }
        )
    if ADS["write_symbol"]:
        points.append(
            {
                "point_id": "plc.setpoint",
                "point_groups": ["telemetry", "control"],
                "address": {"symbol": ADS["write_symbol"]},
                "data_type": "float32",
            }
        )
    system: dict = {"runtime": {"connect_timeout": 10.0, "read_timeout": 5.0}}
    if ADS["local_net_id"] and ADS["local_ip"]:
        system["ads"] = {
            "local_ams_net_id": ADS["local_net_id"],
            "local_ip": ADS["local_ip"],
        }
    return write_config_tree(
        base,
        devices=[
            {
                "device_id": "ads-1",
                "protocol": "ads",
                "point_table": "ads",
                "endpoint": {
                    "host": ADS["host"],
                    "port": ADS["port"],
                    "extensions": {
                        "target_net_id": ADS["net_id"],
                        "target_port": ADS["port"],
                        "timeout": 5.0,
                    },
                },
            }
        ],
        point_tables={"ads": {"points": points}},
        sinks=[{"name": "null_sink", "type": "null"}],
        tasks=[],
        system=system,
    )


@pytest.fixture
async def ads_runtime(tmp_path: Path):
    """真实 PLC 前的已启动 Runtime（连接失败即整模块失败——不降级）。"""
    config_dir = _write_ads_config(tmp_path / "cfg")
    rt = assemble(config_dir, sink_factory=functional_sink_factory)
    await start_runtime(rt)
    try:
        yield rt
    finally:
        await stop_runtime(rt)


class TestRealAdsConnection:
    async def test_device_connects_to_real_plc(self, ads_runtime) -> None:
        result = await ads_runtime.diagnostic.verify_device("ads-1", timeout=3.0)
        stage = next(s for s in result.stages if s.name == "protocol")
        assert stage.ok, stage.message


class TestRealAdsRead:
    async def test_read_symbol_from_real_plc(self, ads_runtime) -> None:
        if not ADS["read_symbol"]:
            pytest.skip("SKIPPED: WIND_HUB_TEST_ADS_READ_SYMBOL not configured")
        value = await ads_runtime.query.read_point("ads-1", "plc.readback")
        assert value.value is not None

    async def test_verify_point_resolves_symbol_address(self, ads_runtime) -> None:
        if not ADS["read_symbol"]:
            pytest.skip("SKIPPED: WIND_HUB_TEST_ADS_READ_SYMBOL not configured")
        result = await ads_runtime.diagnostic.verify_point("ads-1", "plc.readback")
        assert result.resolved_address is not None
        assert result.resolved_address["index_group"] is not None
        assert result.resolved_address["index_offset"] is not None
        assert result.ok, result.error


class TestRealAdsWrite:
    async def test_write_symbol_to_real_plc(self, ads_runtime) -> None:
        if not ADS["write_symbol"]:
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
