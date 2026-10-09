"""Shared topic-scoped configuration adapter contract tests."""

from __future__ import annotations

import hashlib

import pytest

from core.application import ConfigError
from core.application.port import ConfigPort, ConfigTopic
from core.infrastructure.config import (
    YamlConfigAdapter,
    fingerprint_config_set,
    fingerprint_config_topics,
    read_yaml_mapping,
)

ALL_TOPICS = tuple(ConfigTopic)


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


def test_fingerprint_preserves_legacy_nul_delimiters(site):
    expected = hashlib.sha256()
    for path in sorted(site.glob("*.yaml")):
        expected.update(path.name.encode("utf-8"))
        expected.update(b"\0")
        expected.update(path.read_bytes())
        expected.update(b"\0")
    assert fingerprint_config_set(site) == expected.hexdigest()
    assert YamlConfigAdapter(site).fingerprint() == expected.hexdigest()
    history = site / ".history"
    history.mkdir()
    (history / "ignored.yaml").write_text("changed: true", encoding="utf-8")
    assert fingerprint_config_set(site) == expected.hexdigest()


def test_topic_fingerprint_scopes_to_given_topics(site):
    adapter = YamlConfigAdapter(site)
    topics = (ConfigTopic.SYSTEM, ConfigTopic.POINTS)
    assert adapter.fingerprint_topics(topics) == fingerprint_config_topics(site, topics)

    scoped = adapter.fingerprint_topics(topics)
    (site / "tasks.yaml").write_text("changed: true\n", encoding="utf-8")
    assert adapter.fingerprint_topics(topics) == scoped

    (site / "points.yaml").write_text("changed: true\n", encoding="utf-8")
    assert adapter.fingerprint_topics(topics) != scoped


def test_topic_fingerprint_changes_when_topic_file_is_deleted(site):
    adapter = YamlConfigAdapter(site)
    topics = (ConfigTopic.UNITS,)
    scoped = adapter.fingerprint_topics(topics)
    (site / "units.yaml").unlink()
    assert adapter.fingerprint_topics(topics) != scoped


def test_all_topics_fingerprint_differs_from_directory_fingerprint(site):
    """主题指纹只覆盖主题文件；目录指纹还覆盖其它 YAML 文件。"""
    (site / "extra.yaml").write_text("extra: true\n", encoding="utf-8")
    adapter = YamlConfigAdapter(site)
    assert adapter.fingerprint_topics(ALL_TOPICS) != adapter.fingerprint()
