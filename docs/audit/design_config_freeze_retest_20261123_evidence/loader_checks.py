from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import yaml

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT / "src"))

from core.configuration.local_selection import select_local_service
from core.configuration.shared_documents import SharedConfigPaths, SharedDocumentLoader
from core.configuration.shared_snapshot import build_shared_snapshot

CONFIGS = ROOT / "configs"
results = {}


def capture(name, function):
    try:
        value = function()
    except Exception as exc:
        results[name] = {
            "status": "REJECTED",
            "exception_type": type(exc).__name__,
            "message": str(exc),
        }
    else:
        results[name] = {"status": "PASS", "detail": value}


def select_prod(service_version: str | None):
    if service_version is None:
        os.environ.pop("WIND_HUB_DEPLOYMENT_VERSION", None)
    else:
        os.environ["WIND_HUB_DEPLOYMENT_VERSION"] = service_version
    selection = select_local_service(
        CONFIGS / "services/service_collector-realtime_v1.1.0.yaml",
        deployment_id="prod-a",
    )
    return {
        "deployment_path": str(selection.deployment_path.relative_to(ROOT)),
        "instances": {
            role: str(path.relative_to(ROOT)) for role, path in selection.instances.items()
        },
    }


def load_devices(filename: str, selected=None, build=False):
    loader = SharedDocumentLoader(
        SharedConfigPaths(
            instances=CONFIGS / "instances",
            templates=CONFIGS / "templates",
        )
    )
    documents = loader.load_devices(
        CONFIGS / "instances" / filename,
        selected_device_ids=selected,
    )
    detail = {
        "devices_file_version": documents.devices_file_version,
        "device_ids": sorted(documents.devices),
        "business_point_catalog_version": documents.business_points.get("file_version"),
        "business_point_count": len(documents.business_points.get("business_points", {})),
    }
    if build:
        site = yaml.safe_load(
            (CONFIGS / "instances/site_example_site_v1.1.0.yaml").read_text(encoding="utf-8")
        )
        documents = replace(documents, site=site)
        snapshot = build_shared_snapshot(documents)
        detail["point_table_point_counts"] = {
            str(key): len(value.points) for key, value in snapshot.point_tables.items()
        }
        detail["source_manifest_count"] = len(snapshot.source_manifest)
    return detail


capture("prod_a_without_deployment_version", lambda: select_prod(None))
capture("prod_a_with_deployment_version_1_4_0", lambda: select_prod("1.4.0"))
capture("farm_a_v1_5_0_all_devices", lambda: load_devices("device_farm-a_v1.5.0.yaml"))
capture(
    "farm_a_v1_5_0_gw2mw_only_with_domain_snapshot",
    lambda: load_devices(
        "device_farm-a_v1.5.0.yaml",
        selected=[f"GW{index:02d}" for index in range(1, 6)],
        build=True,
    ),
)
capture("farm_a_v1_4_0_historical", lambda: load_devices("device_farm-a_v1.4.0.yaml"))
capture("e2e_final_v1_0_0", lambda: load_devices("device_e2e-final_v1.0.0.yaml"))
capture("sim_v1_2_0", lambda: load_devices("device_sim_v1.2.0.yaml"))

print(json.dumps(results, ensure_ascii=False, indent=2))
expected = {
    "prod_a_without_deployment_version": "REJECTED",
    "prod_a_with_deployment_version_1_4_0": "PASS",
    "farm_a_v1_5_0_all_devices": "REJECTED",
    "farm_a_v1_5_0_gw2mw_only_with_domain_snapshot": "PASS",
    "farm_a_v1_4_0_historical": "REJECTED",
    "e2e_final_v1_0_0": "REJECTED",
    "sim_v1_2_0": "REJECTED",
}
sys.exit(0 if all(results.get(key, {}).get("status") == value for key, value in expected.items()) else 1)
