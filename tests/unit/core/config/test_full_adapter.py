"""新配置入口的真实 YAML 加载测试。"""

from pathlib import Path

import pytest

from core.infrastructure.config.full_adapter import FullYamlConfigAdapter


ROOT = Path(__file__).resolve().parents[4]


def test_load_example_modbus_without_units_file() -> None:
    config = FullYamlConfigAdapter(ROOT / "configs" / "example_modbus").load()
    assert type(config).__name__ == "ConfigSnapshot"
    assert config.business_points["active_power"].description == "有功功率"
    assert config.point_tables["wtg_modbus_site_v1"].points["active_power"].business_point_id == "active_power"
    assert config.system.site_id == "example_modbus"
    assert config.devices
    assert config.device_models
    assert config.point_tables
    assert config.tasks
    assert config.sinks
    assert all(device.device_group_ids for device in config.devices.values())

    task = config.tasks["turbine-modbus-all"]
    assert task.device_group_id == "turbine_modbus"
    assert task.sink_ids == ("file_archive",)


def test_full_loader_rejects_absent_yaml(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="Configuration file not found"):
        FullYamlConfigAdapter(tmp_path).load()


def test_full_loader_requires_business_point_catalog(tmp_path: Path) -> None:
    """不再隐式合成 BusinessPoint；目录必须提供 business_points.yaml。"""
    import shutil

    source = ROOT / "configs" / "example_modbus"
    for filename in (
        "system.yaml",
        "device_models.yaml",
        "devices.yaml",
        "points.yaml",
        "tasks.yaml",
        "sinks.yaml",
    ):
        shutil.copyfile(source / filename, tmp_path / filename)
    with pytest.raises(Exception, match="business_points.yaml"):
        FullYamlConfigAdapter(tmp_path).load()
