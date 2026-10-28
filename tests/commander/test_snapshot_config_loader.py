"""Commander 配置入口直接消费 Core 完整领域快照。"""

from pathlib import Path

import pytest

from commander.infrastructure.config.loader import load_commander_config
from core.infrastructure.config import YamlConfigAdapter
from tests.support.new_commander import write_minimal_config_tree

pytestmark = pytest.mark.unit


def test_commander_loads_domain_snapshot(tmp_path: Path) -> None:
    base = write_minimal_config_tree(tmp_path / "cfg")
    assert (base / "units.yaml").exists()
    snapshot = YamlConfigAdapter(base).load()
    commander = load_commander_config(YamlConfigAdapter(base))
    assert commander.point_tables == snapshot.point_tables
    assert commander.device_models == snapshot.device_models
    assert commander.business_points == snapshot.business_points
    assert commander.devices == {
        key: device for key, device in snapshot.site.devices.items() if device.enabled
    }


def test_commander_configport_source_is_full_snapshot(tmp_path: Path) -> None:
    snapshot = YamlConfigAdapter(write_minimal_config_tree(tmp_path / "cfg")).load()

    class Source:
        def load(self):
            return snapshot

        def save(self, value):
            raise AssertionError("read-only loading must not write")

    config = load_commander_config(Source())
    assert set(config.devices).issubset(snapshot.site.devices)
    for device_id in config.devices:
        assert config.point_table_for_device(device_id)
