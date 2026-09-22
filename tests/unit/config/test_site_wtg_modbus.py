"""site_wtg_modbus 现场配置加载与点映射测试。

验证 configs/site_wtg_modbus 能被 load_config 直接加载，且设备 / 点表 /
Task 定义满足现场要求（26 台 Modbus 风机、单一共享点表、单一采集 Task）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wind_hub.adapter.outbound.protocol.modbus.mapping import parse_point
from wind_hub.config.loader import load_config

SITE_DIR = Path(__file__).resolve().parents[3] / "configs" / "site_wtg_modbus"

EXPECTED_DEVICE_IDS = {
    "wtg-002",
    "wtg-003",
    "wtg-004",
    "wtg-011",
    "wtg-042",
    "wtg-043",
    "wtg-044",
    "wtg-045",
    "wtg-046",
    "wtg-047",
    "wtg-049",
    "wtg-050",
    "wtg-051",
    "wtg-052",
    "wtg-054",
    "wtg-055",
    "wtg-056",
    "wtg-057",
    "wtg-058",
    "wtg-059",
    "wtg-060",
    "wtg-061",
    "wtg-069",
    "wtg-070",
    "wtg-071",
    "wtg-072",
}

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
        assert site_config.reporting is None  # 现场配置不启用 IEC104 slave proxy

    def test_26_devices_all_present(self, site_config) -> None:
        ids = {d.device_id for d in site_config.devices.devices}
        assert len(ids) == 26
        assert ids == EXPECTED_DEVICE_IDS

    def test_all_devices_same_group_and_table(self, site_config) -> None:
        for d in site_config.devices.devices:
            assert d.protocol == "modbus"
            assert d.device_group == "turbine_modbus"
            assert d.point_table == "wtg_modbus_site_v1"
            assert d.enabled is True
            assert d.endpoint.port == 502

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

    def test_task_expands_to_26_instances(self, site_config) -> None:
        """device_group Task 展开数量 = 命中的 enabled 设备数（Runtime 语义）。"""
        task = site_config.tasks.tasks[0]
        matched = [
            d
            for d in site_config.devices.devices
            if d.enabled and d.device_group == task.device_group
        ]
        assert len(matched) == 26

    def test_all_group_has_only_8_acquisition_points(self, site_config) -> None:
        points = site_config.point_tables.tables["wtg_modbus_site_v1"].points
        all_group = {p.point_id for p in points if "all" in p.point_groups}
        assert all_group == ACQUISITION_POINTS
        # 控制点不得进入 all 组（否则周期任务会去读控制寄存器）
        assert all_group.isdisjoint(CONTROL_POINTS)
        control_group = {p.point_id for p in points if "control" in p.point_groups}
        assert control_group == CONTROL_POINTS

    def test_file_archive_sink_enabled(self, site_config) -> None:
        sinks = {s.name: s for s in site_config.system.sinks}
        assert sinks["file_archive"].enabled is True
        assert sinks["file_archive"].params["path"] == "/var/tmp/wind-hub/archive.jsonl"
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
        for pid in (
            "active_power",
            "active_power_1min",
            "reactive_power",
            "wind_speed",
            "generator_speed",
            "pitch_angle",
        ):
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
