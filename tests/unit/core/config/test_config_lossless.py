"""完整 Core YAML 配置关键不变量与无损往返。"""

from dataclasses import replace
from pathlib import Path
from shutil import copy2

import yaml

from core.application.config_diff import diff_config_snapshots
from core.infrastructure.config.adapter import YamlConfigAdapter
from core.infrastructure.config.digest import config_dir_digest
from core.infrastructure.config.yaml import active_config_dir

ROOT = Path(__file__).resolve().parents[4]
FILES = (
    "system.yaml", "device_models.yaml", "devices.yaml", "points.yaml",
    "business_points.yaml", "tasks.yaml", "sinks.yaml",
)


def _fixture(tmp_path: Path, example: str) -> YamlConfigAdapter:
    for name in FILES:
        copy2(ROOT / "configs" / example / name, tmp_path / name)
    return YamlConfigAdapter(tmp_path)


def test_device_model_options_and_system_interfaces_survive_save(tmp_path: Path) -> None:
    adapter = _fixture(tmp_path, "example_modbus")
    before = adapter.load()
    model = next(iter(before.device_models.values()))
    assert model.connection_defaults["port"] == 502
    adapter.save(before)
    after = adapter.load()
    assert not diff_config_snapshots(before, after).has_changes
    system = yaml.safe_load((active_config_dir(tmp_path) / "system.yaml").read_text())
    assert system["interfaces"]["api"]["enabled"] is True


def test_repeated_save_does_not_hash_inactive_generations(tmp_path: Path) -> None:
    adapter = _fixture(tmp_path, "example_modbus")
    before = adapter.load()
    adapter.save(before)
    first_digest = config_dir_digest(tmp_path)
    adapter.save(before)
    assert config_dir_digest(tmp_path) == first_digest


def test_ads_subscription_option_round_trip(tmp_path: Path) -> None:
    adapter = _fixture(tmp_path, "example_ads")
    devices = tmp_path / "devices.yaml"
    document = yaml.safe_load(devices.read_text())
    document["devices"][0]["endpoint"].setdefault("extensions", {})["subscribe_enabled"] = True
    devices.write_text(yaml.safe_dump(document, sort_keys=False))
    before = adapter.load()
    assert before.ads_subscribe_devices
    adapter.save(before)
    after = adapter.load()
    assert after.ads_subscribe_devices == before.ads_subscribe_devices
    assert not diff_config_snapshots(before, after).has_changes


def test_removing_ads_settings_does_not_restore_old_yaml(tmp_path: Path) -> None:
    adapter = _fixture(tmp_path, "example_ads")
    current = adapter.load()
    if current.system.ads is None:
        return
    modified = replace(current, system=replace(current.system, ads=None))
    adapter.save(modified)
    restored = adapter.load()
    assert restored.system.ads is None
    system = yaml.safe_load((active_config_dir(tmp_path) / "system.yaml").read_text())
    assert "ads" not in system
