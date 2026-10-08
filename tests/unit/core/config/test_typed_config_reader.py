"""Core 类型化配置读取契约测试。"""

from __future__ import annotations

import pytest

from core.application import ConfigError
from core.application.port import TypedConfigReader
from core.infrastructure.config import YamlTypedConfigAdapter


def test_typed_reader_returns_independent_validated_topics(tmp_path):
    files = {
        "device_models": "device_types: {}\ndevice_models: {}\n",
        "devices": "devices: []\n",
        "points": "point_tables: {}\n",
        "units": "units: {}\n",
        "tasks": "tasks: []\n",
    }
    for name, body in files.items():
        (tmp_path / f"{name}.yaml").write_text(body, encoding="utf-8")

    reader = YamlTypedConfigAdapter(tmp_path)
    assert isinstance(reader, TypedConfigReader)
    assert reader.read_device_config().instances.devices == []
    assert reader.read_device_config().models.device_models == {}
    assert reader.read_point_config().tables == {}
    assert reader.read_task_config().definition.tasks == []
    assert reader.read_unit_config().definition.units == {}


def test_invalid_task_is_rejected_without_loading_other_topics(tmp_path):
    (tmp_path / "tasks.yaml").write_text(
        "tasks:\n"
        "  - task_id: bad\n"
        "    device: wt01\n"
        "    device_group: wind\n"
        "    point_group: fast\n"
        "    targets: [{sink: archive}]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="exactly one"):
        YamlTypedConfigAdapter(tmp_path).read_task_config()


def test_point_config_expands_inheritance(tmp_path):
    (tmp_path / "points.yaml").write_text(
        "point_tables:\n"
        "  base:\n"
        "    protocol: modbus\n"
        "    points:\n"
        "      - point_id: power\n"
        "        point_groups: [fast]\n"
        "        address: {type: holding, offset: 0}\n"
        "  child:\n"
        "    extends: base\n",
        encoding="utf-8",
    )
    points = YamlTypedConfigAdapter(tmp_path).read_point_config()
    assert "power" in points.tables["child"].points
