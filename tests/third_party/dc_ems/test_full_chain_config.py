"""完整测量链路的配置和映射回归，不要求 DCEMS 镜像。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.third_party.dc_ems.check_full_chain import POINTS, check
from tests.third_party.dc_ems.prepare_full_chain import STATION_POINTS, prepare
from tests.third_party.dc_ems.prepare_full_chain_cli import prepare_cli


def test_full_chain_production_loaders(tmp_path: Path) -> None:
    """逐项加载真实 Gateway、Collector、DG、Sink 和 Deployment。"""
    root = tmp_path / "dcems-measurement"
    config = prepare(root)
    check(root)
    cli = yaml.safe_load(
        prepare_cli(config, root / "state").read_text(encoding="utf-8")
    )
    assert set(cli["services"]) == {
        "gateway", "gateway-target", "collector-realtime", "sink", "data-generator"
    }
    assert cli["services"]["sink"]["process"]["argv"][-1].endswith(
        "service_sink-dcems-measurement_v2.0.0.yaml"
    )
    assert cli["services"]["collector-realtime"]["process"]["argv"][2] == "collector.main"


def test_isolated_templates_and_no_target_collection(tmp_path: Path) -> None:
    """站控只属于首台；目标设备必须和 Collector 源组隔离。"""
    root = tmp_path / "dcems-measurement"
    config = prepare(root)
    templates = config / "templates"
    full = yaml.safe_load(
        (templates / "point_tables" / "point_table_dcems_wtg_random_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )["points"]
    compact = yaml.safe_load(
        (
            templates / "point_tables"
            / "point_table_dcems_wtg_measurement_v1.0.0.yaml"
        ).read_text(encoding="utf-8")
    )["points"]
    assert set(full) == POINTS | STATION_POINTS
    assert set(compact) == POINTS
    sources = yaml.safe_load(
        (config / "instances" / "device_dcems-source_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )["devices"]
    targets = yaml.safe_load(
        (config / "instances" / "device_dcems-target_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )["devices"]
    assert {value["group"] for value in sources.values()} == {"dcems-source"}
    assert {value["group"] for value in targets.values()} == {"dcems-target"}
    assert sources["WTG01"]["model"]["id"] == "dcems_wtg_random"
    assert all(
        sources[f"WTG{index:02d}"]["model"]["id"] == "dcems_wtg_measurement"
        for index in range(2, 11)
    )
    tasks = yaml.safe_load(
        (config / "instances" / "collector_tasks_dcems-source_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )["tasks"]
    assert {value["group"] for value in tasks.values()} == {"dcems-source"}


def test_full_chain_refuses_overwrite(tmp_path: Path) -> None:
    """禁止覆盖已启动或已生成的隔离配置与运行状态。"""
    root = tmp_path / "dcems-measurement"
    prepare(root)
    with pytest.raises(FileExistsError):
        prepare(root)
