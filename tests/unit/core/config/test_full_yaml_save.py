"""完整 YAML 保存以及快照一致性测试。"""

from pathlib import Path
from shutil import copy2

from core.application.config_diff import diff_config_snapshots
from core.infrastructure.config.adapter import YamlConfigAdapter

ROOT = Path(__file__).resolve().parents[4]


def test_full_yaml_save_load_round_trip(tmp_path: Path) -> None:
    source = ROOT / "configs" / "example_modbus"
    for name in (
        "system.yaml", "device_models.yaml", "devices.yaml", "points.yaml",
        "business_points.yaml", "tasks.yaml", "sinks.yaml",
    ):
        copy2(source / name, tmp_path / name)
    adapter = YamlConfigAdapter(tmp_path)
    before = adapter.load()
    adapter.save(before)
    after = adapter.load()
    assert not diff_config_snapshots(before, after).has_changes
    assert after.site.devices == before.site.devices
