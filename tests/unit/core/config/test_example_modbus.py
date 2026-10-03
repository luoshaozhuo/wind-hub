"""example_modbus 示例配置加载与点映射测试。

验证 configs/example_modbus 能被 load_config 直接加载：设备实例经
device_models.yaml 的型号 wtg_modbus_site resolve 为完整 DeviceConfig，
点表（points.yaml 的 wtg_modbus_site_v1）与 Task 定义满足现场要求。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wind_hub_core.config.loader import load_config
from wind_hub_core.protocol.modbus.mapping import parse_point

SITE_DIR = Path(__file__).resolve().parents[4] / "configs" / "example_modbus"

EXPECTED_DEVICE_IDS = {"wtg-002", "wtg-003"}

ACQUISITION_POINTS = {
    "active_power",
    "active_power_1min",
    "reactive_power",
    "wind_speed",
    "generator_speed",
    "pitch_angle",
    "fault_code",
    "turbine_status",
}

CONTROL_POINTS = {
    "active_power_setpoint",
    "reactive_power_setpoint",
    "turbine_command",
}


@pytest.fixture(scope="module")
def site_config():
    return load_config(SITE_DIR)


class TestSiteConfigLoading:
    def test_load_config_succeeds(self, site_config) -> None:
        assert site_config.system.site is not None
        assert site_config.system.site.site_id == "example_modbus"

    def test_devices_all_present(self, site_config) -> None:
        ids = {d.device_id for d in site_config.devices.devices}
        assert ids == EXPECTED_DEVICE_IDS

    def test_devices_resolved_from_model(self, site_config) -> None:
        """实例不配置协议/点表——resolve 后来自型号 wtg_modbus_site。"""
        model = site_config.device_models["wtg_modbus_site"]
        assert model.protocol == "modbus"
        assert model.point_table == "wtg_modbus_site_v1"
        assert model.device_type == "turbine"
        for d in site_config.devices.devices:
            assert d.model == "wtg_modbus_site"
            assert d.device_type == "turbine"
            assert d.protocol == "modbus"
            assert d.device_group == "turbine_modbus"
            assert d.point_table == "wtg_modbus_site_v1"
            assert d.enabled is True
            # 连接默认值自型号 connection_defaults 合并
            assert d.endpoint.port == 502
            assert d.endpoint.extensions["unit_id"] == 1
            assert d.endpoint.extensions["word_order"] == "little_endian"

    def test_single_task(self, site_config) -> None:
        tasks = site_config.tasks.tasks
        assert len(tasks) == 1
        task = tasks[0]
        assert task.task_id == "turbine-modbus-all"
        assert task.device_group == "turbine_modbus"
        assert task.point_group == "all"
        assert task.interval == 1.0
        assert task.enabled is True
        assert [t.sink for t in task.targets] == ["file_archive"]

    def test_task_expands_to_all_devices(self, site_config) -> None:
        """device_group Task 展开数量 = 命中的 enabled 设备数（Runtime 语义）。"""
        task = site_config.tasks.tasks[0]
        matched = [
            d
            for d in site_config.devices.devices
            if d.enabled and d.device_group == task.device_group
        ]
        assert len(matched) == len(EXPECTED_DEVICE_IDS)

    def test_all_group_has_only_8_acquisition_points(self, site_config) -> None:
        points = site_config.point_tables.tables["wtg_modbus_site_v1"].points
        all_group = {p.point_id for p in points if "all" in p.point_groups}
        assert all_group == ACQUISITION_POINTS
        # 控制点不得进入 all 组（否则周期任务会去读控制寄存器）
        assert all_group.isdisjoint(CONTROL_POINTS)
        control_group = {p.point_id for p in points if "control" in p.point_groups}
        assert control_group == CONTROL_POINTS

    def test_file_archive_sink_enabled(self, site_config) -> None:
        sinks = {s.name: s for s in site_config.sinks.sinks}
        assert sinks["file_archive"].enabled is True
        assert sinks["file_archive"].connection.path == "/var/tmp/wind-hub/archive.jsonl"
        assert sinks["kafka_main"].enabled is False
        assert sinks["db_main"].enabled is False


class TestSitePointMapping:
    """FC04 → input；S32 → 2 registers；S16 → 1 register；scale 正确。"""

    def test_fc04_points_parse_as_input(self, site_config) -> None:
        points = site_config.point_tables.tables["wtg_modbus_site_v1"].points
        for p in points:
            if p.point_id not in ACQUISITION_POINTS:
                continue
            mp = parse_point(p)
            assert mp.register_type == "input", p.point_id

    def test_control_points_parse_as_holding(self, site_config) -> None:
        points = site_config.point_tables.tables["wtg_modbus_site_v1"].points
        for p in points:
            if p.point_id not in CONTROL_POINTS:
                continue
            mp = parse_point(p)
            assert mp.register_type == "holding", p.point_id

    def test_int32_occupies_2_registers(self, site_config) -> None:
        points = site_config.point_tables.tables["wtg_modbus_site_v1"].points
        for p in points:
            mp = parse_point(p)
            if p.data_type == "int32":
                assert mp.count == 2, p.point_id
            elif p.data_type == "int16":
                assert mp.count == 1, p.point_id

    def test_scales(self, site_config) -> None:
        points = {
            p.point_id: p for p in site_config.point_tables.tables["wtg_modbus_site_v1"].points
        }
        for pid in ("active_power", "active_power_1min", "reactive_power"):
            assert points[pid].scale == pytest.approx(0.001), pid
        for pid in ("wind_speed", "generator_speed", "pitch_angle"):
            assert points[pid].scale == pytest.approx(0.01), pid
        for pid in ("fault_code", "turbine_status", *CONTROL_POINTS):
            assert points[pid].scale == pytest.approx(1.0), pid

    def test_addresses(self, site_config) -> None:
        points = {
            p.point_id: parse_point(p)
            for p in site_config.point_tables.tables["wtg_modbus_site_v1"].points
        }
        expected = {
            "active_power": 178,
            "active_power_1min": 204,
            "reactive_power": 180,
            "wind_speed": 357,
            "generator_speed": 104,
            "pitch_angle": 859,
            "fault_code": 19,
            "turbine_status": 0,
            "active_power_setpoint": 2001,
            "reactive_power_setpoint": 2003,
            "turbine_command": 2005,
        }
        for pid, addr in expected.items():
            assert points[pid].address == addr, pid
