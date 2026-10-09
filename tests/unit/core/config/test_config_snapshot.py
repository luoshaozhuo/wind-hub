"""一致性配置快照契约测试。"""

from __future__ import annotations

import pytest

from core.application import ConfigError
from core.application.port import ConfigSnapshot, ConfigTopic
from core.infrastructure.config import YamlConfigAdapter

TOPICS = (ConfigTopic.POINTS, ConfigTopic.UNITS)


@pytest.fixture
def site(tmp_path):
    (tmp_path / "points.yaml").write_text(
        "point_tables:\n"
        "  main:\n"
        "    protocol: modbus\n"
        "    points:\n"
        "      - point_id: power\n"
        "        point_groups: [g]\n"
        "        address: {register_type: holding, address: 0}\n",
        encoding="utf-8",
    )
    (tmp_path / "units.yaml").write_text("units: {}\n", encoding="utf-8")
    (tmp_path / "sinks.yaml").write_text("sinks: []\n", encoding="utf-8")
    return tmp_path


def test_snapshot_implements_port_and_reads_declared_topics(site):
    snapshot = YamlConfigAdapter(site).open_snapshot(TOPICS)
    assert isinstance(snapshot, ConfigSnapshot)
    assert "power" in snapshot.read(ConfigTopic.POINTS).tables["main"].points
    assert snapshot.read(ConfigTopic.UNITS).units == {}
    snapshot.verify_unchanged()


def test_snapshot_reads_are_immune_to_concurrent_modification(site):
    """会话内读取解析自 open 时捕获的内容，不混用不同版本。"""
    snapshot = YamlConfigAdapter(site).open_snapshot(TOPICS)
    (site / "points.yaml").write_text("point_tables: {}\n", encoding="utf-8")
    assert "main" in snapshot.read(ConfigTopic.POINTS).tables


def test_verify_unchanged_detects_concurrent_modification(site):
    snapshot = YamlConfigAdapter(site).open_snapshot(TOPICS)
    (site / "points.yaml").write_text("point_tables: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="changed while reading snapshot"):
        snapshot.verify_unchanged()


def test_undeclared_topic_read_is_rejected(site):
    """未声明主题不会隐式读盘；依赖范围显式。"""
    snapshot = YamlConfigAdapter(site).open_snapshot(TOPICS)
    with pytest.raises(ConfigError, match="does not include"):
        snapshot.read(ConfigTopic.SINKS)


def test_empty_topics_are_rejected(site):
    with pytest.raises(ConfigError, match="at least one topic"):
        YamlConfigAdapter(site).open_snapshot(())
