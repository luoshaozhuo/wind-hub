"""装配 dcems-smoke 的独立配置根，不影响生产配置。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path(__file__).resolve().parent / "config"
TEMPLATES = ROOT / "deploy" / "config_roots" / "e2e-final" / "templates"

ASSETS = {
    "business_points_standard_v1.6.0.yaml": "templates",
    "point_table_dcems_wtg_random_v1.0.0.yaml": "templates/point_tables",
    "device_model_dcems_wtg_random_v1.0.0.yaml": "templates/device_models",
    "site_dcems-smoke_v1.0.0.yaml": "instances",
    "device_dcems-smoke_v1.0.0.yaml": "instances",
    "scenarios_dcems-smoke_v1.0.0.yaml": "instances",
    "service_gateway-dcems-smoke_v1.0.0.yaml": "services",
    "service_data-generator-dcems-smoke_v1.0.0.yaml": "services",
    "deployment_dcems-smoke_v1.0.0.yaml": "deployments",
}


def stage(target: Path) -> None:
    """仅允许向新的隔离目录写入；不复用已发布 root。"""
    if target.exists():
        raise FileExistsError(f"configuration root already exists: {target}")
    if not TEMPLATES.is_dir():
        raise FileNotFoundError(TEMPLATES)
    if target.resolve() == ROOT or ROOT == target.resolve():
        raise ValueError("configuration root cannot be the repository root")
    target.mkdir(parents=True)
    shutil.copytree(TEMPLATES, target / "templates")
    for filename, subdir in ASSETS.items():
        source = SOURCE / filename
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = target / subdir / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    # 原标准模板中的历史型号保留为独立只读配置依赖，
    # 仅部署清单选择 dcems-smoke 服务和实例。


def main() -> None:
    """生成配置根，供官方 Loader 执行完整校验。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    stage(args.output.resolve())


if __name__ == "__main__":
    main()
