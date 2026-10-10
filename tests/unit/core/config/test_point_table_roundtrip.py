"""完整领域点表保存后重新加载的往返测试。"""

from pathlib import Path
from shutil import copyfile

from core.infrastructure.config.full_adapter import FullYamlConfigAdapter


ROOT = Path(__file__).resolve().parents[4]


def test_point_tables_yaml_round_trip(tmp_path: Path) -> None:
    source = ROOT / "configs" / "template"
    for filename in (
        "system.yaml", "device_models.yaml", "devices.yaml",
        "points.yaml", "tasks.yaml", "sinks.yaml", "business_points.yaml",
    ):
        copyfile(source / filename, tmp_path / filename)

    adapter = FullYamlConfigAdapter(tmp_path)
    before = adapter.load()
    adapter.save_point_tables(before)
    after = adapter.load()

    assert before.point_tables == after.point_tables
    assert before.point_meta == after.point_meta
    assert all(
        before.point_tables[key].parent_id == after.point_tables[key].parent_id
        for key in before.point_tables
    )

    written = (tmp_path / "points.yaml").read_text(encoding="utf-8")
    assert "extends: beckhoff_base_v1" in written
    assert "business_point_id" in written
