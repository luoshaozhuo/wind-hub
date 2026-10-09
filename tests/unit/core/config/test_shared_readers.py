"""Shared topic-scoped configuration reader contract tests."""

from __future__ import annotations

import hashlib

import pytest

from core.application import ConfigError
from core.application.port import ConfigReader, ConfigTopic
from core.infrastructure.config import (
    YamlConfigReader,
    YamlTypedConfigAdapter,
    fingerprint_config_set,
    fingerprint_config_topics,
)

ALL_TOPICS = tuple(ConfigTopic)


@pytest.fixture
def site(tmp_path):
    for name in (
        "system",
        "device_models",
        "devices",
        "points",
        "units",
        "tasks",
        "sinks",
    ):
        (tmp_path / f"{name}.yaml").write_text(
            f"section: {name}\n",
            encoding="utf-8",
        )
    return tmp_path


def test_adapter_implements_config_reader_port(site):
    reader = YamlTypedConfigAdapter(site)
    assert isinstance(reader, ConfigReader)


def test_raw_reader_reads_each_topic_independently(site):
    reader = YamlConfigReader(site)
    assert reader.read_system() == {"section": "system"}
    assert reader.read_device_models() == {"section": "device_models"}
    assert reader.read_devices() == {"section": "devices"}
    assert reader.read_points() == {"section": "points"}
    assert reader.read_units() == {"section": "units"}
    assert reader.read_tasks() == {"section": "tasks"}
    assert reader.read_sinks() == {"section": "sinks"}


def test_reading_missing_invalid_or_empty_file_is_rejected(tmp_path):
    reader = YamlConfigReader(tmp_path)
    with pytest.raises(ConfigError, match="not found"):
        reader.read_devices()
    path = tmp_path / "devices.yaml"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="root must be a mapping"):
        reader.read_devices()
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="Empty configuration"):
        reader.read_devices()
    path.write_text("devices: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        reader.read_devices()


def test_missing_unrelated_topic_does_not_block_typed_read(tmp_path):
    """缺失无关主题文件（如 sinks/tasks）不影响当前主题读取。"""
    (tmp_path / "units.yaml").write_text("units: {}\n", encoding="utf-8")
    reader = YamlTypedConfigAdapter(tmp_path)
    assert reader.read_unit_config().units == {}
    with pytest.raises(ConfigError, match="not found"):
        reader.read_sink_config()


def test_fingerprint_preserves_legacy_nul_delimiters(site):
    expected = hashlib.sha256()
    for path in sorted(site.glob("*.yaml")):
        expected.update(path.name.encode("utf-8"))
        expected.update(b"\0")
        expected.update(path.read_bytes())
        expected.update(b"\0")
    assert fingerprint_config_set(site) == expected.hexdigest()
    assert YamlConfigReader(site).fingerprint() == expected.hexdigest()
    history = site / ".history"
    history.mkdir()
    (history / "ignored.yaml").write_text("changed: true", encoding="utf-8")
    assert fingerprint_config_set(site) == expected.hexdigest()


def test_topic_fingerprint_scopes_to_given_topics(site):
    reader = YamlConfigReader(site)
    topics = (ConfigTopic.SYSTEM, ConfigTopic.POINTS)
    assert reader.fingerprint_topics(topics) == fingerprint_config_topics(site, topics)

    scoped = reader.fingerprint_topics(topics)
    (site / "tasks.yaml").write_text("changed: true\n", encoding="utf-8")
    assert reader.fingerprint_topics(topics) == scoped

    (site / "points.yaml").write_text("changed: true\n", encoding="utf-8")
    assert reader.fingerprint_topics(topics) != scoped


def test_topic_fingerprint_changes_when_topic_file_is_deleted(site):
    reader = YamlConfigReader(site)
    topics = (ConfigTopic.UNITS,)
    scoped = reader.fingerprint_topics(topics)
    (site / "units.yaml").unlink()
    assert reader.fingerprint_topics(topics) != scoped


def test_all_topics_fingerprint_differs_from_directory_fingerprint(site):
    """主题指纹只覆盖主题文件；目录指纹还覆盖其它 YAML 文件。"""
    (site / "extra.yaml").write_text("extra: true\n", encoding="utf-8")
    reader = YamlConfigReader(site)
    assert reader.fingerprint_topics(ALL_TOPICS) != reader.fingerprint()
