"""Shared topic-scoped configuration adapter contract tests."""

from __future__ import annotations

import pytest

from core.application import ConfigError
from core.application.port import ConfigPort, ConfigTopic
from core.infrastructure.config import (
    YamlConfigAdapter,
    read_yaml_mapping,
)


@pytest.fixture
def site(tmp_path):
    (tmp_path / "system.yaml").write_text("site: {site_id: s1}\n", encoding="utf-8")
    (tmp_path / "device_models.yaml").write_text(
        "device_types: {turbine: {}}\ndevice_models: {}\n", encoding="utf-8"
    )
    (tmp_path / "devices.yaml").write_text("devices: []\n", encoding="utf-8")
    (tmp_path / "points.yaml").write_text("point_tables: {}\n", encoding="utf-8")
    (tmp_path / "units.yaml").write_text("units: {}\n", encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    (tmp_path / "sinks.yaml").write_text("sinks: []\n", encoding="utf-8")
    return tmp_path


def test_adapter_implements_config_port(site):
    adapter = YamlConfigAdapter(site)
    assert isinstance(adapter, ConfigPort)


def test_adapter_reads_each_topic_independently(site):
    adapter = YamlConfigAdapter(site)
    assert adapter.read(ConfigTopic.SYSTEM).site.site_id == "s1"
    assert "turbine" in adapter.read(ConfigTopic.DEVICE_MODELS).device_types
    assert adapter.read(ConfigTopic.DEVICES).devices == ()
    assert adapter.read(ConfigTopic.POINTS).tables == {}
    assert adapter.read(ConfigTopic.UNITS).units == {}
    assert adapter.read(ConfigTopic.TASKS).tasks == ()
    assert adapter.read(ConfigTopic.SINKS).sinks == []


def test_reading_missing_invalid_or_empty_file_is_rejected(tmp_path):
    adapter = YamlConfigAdapter(tmp_path)
    with pytest.raises(ConfigError, match="not found"):
        adapter.read(ConfigTopic.DEVICES)
    path = tmp_path / "devices.yaml"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="root must be a mapping"):
        adapter.read(ConfigTopic.DEVICES)
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="Empty configuration"):
        adapter.read(ConfigTopic.DEVICES)
    path.write_text("devices: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        adapter.read(ConfigTopic.DEVICES)


def test_missing_unrelated_topic_does_not_block_read(tmp_path):
    """缺失无关主题文件（如 sinks/tasks）不影响当前主题读取。"""
    (tmp_path / "units.yaml").write_text("units: {}\n", encoding="utf-8")
    adapter = YamlConfigAdapter(tmp_path)
    assert adapter.read(ConfigTopic.UNITS).units == {}
    with pytest.raises(ConfigError, match="not found"):
        adapter.read(ConfigTopic.SINKS)


def test_read_yaml_mapping_low_level(tmp_path):
    path = tmp_path / "devices.yaml"
    path.write_text("devices: []\n", encoding="utf-8")
    assert read_yaml_mapping(path) == {"devices": []}
