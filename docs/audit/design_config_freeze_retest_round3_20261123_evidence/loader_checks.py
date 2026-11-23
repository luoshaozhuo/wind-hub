from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(REPO / "src"))

from core.configuration.local_selection import select_local_service
from core.configuration.shared_documents import SharedConfigPaths, SharedDocumentLoader
from core.configuration.shared_snapshot import build_shared_snapshot

CASES = {
    "prod-a": {
        "deployment_version": "1.6.0",
        "service": "service_collector-realtime_v1.1.0.yaml",
        "site": "site_example_site_v1.1.0.yaml",
        "devices": "device_farm-a_v1.6.0.yaml",
        "expected_devices": 10,
        "expected_points": {
            "point_table_ads_wtg_gw2mw_v1.2.0": 111,
            "point_table_modbus_wtg_tz2mw_v1.2.0": 10,
        },
    },
    "sim-a": {
        "deployment_version": "1.4.0",
        "service": "service_collector-sim_v1.0.0.yaml",
        "site": "site_example_site_v1.1.0.yaml",
        "devices": "device_sim_v1.4.0.yaml",
        "expected_devices": 5,
        "expected_points": {
            "point_table_modbus_wtg_sim_v1.2.0": 32,
            "point_table_modbus_pv_sim_v1.2.0": 16,
            "point_table_modbus_bess_sim_v1.2.0": 20,
            "point_table_modbus_grid_sim_v1.2.0": 15,
            "point_table_modbus_plant_controller_sim_v1.2.0": 20,
        },
    },
    "e2e-final": {
        "deployment_version": "1.0.1",
        "service": "service_collector-e2e-realtime_v1.0.0.yaml",
        "site": "site_e2e-final_v1.0.0.yaml",
        "devices": "device_e2e-final_v1.0.1.yaml",
        "expected_devices": 6,
        "expected_points": {
            "point_table_modbus_wtg_sim_v1.2.0": 32,
            "point_table_modbus_pv_sim_v1.2.0": 16,
            "point_table_modbus_bess_sim_v1.2.0": 20,
            "point_table_modbus_grid_sim_v1.2.0": 15,
            "point_table_modbus_plant_controller_sim_v1.2.0": 20,
        },
    },
}

results = {}
failed = []
for domain, case in CASES.items():
    root = REPO / "deploy/config_roots" / domain
    try:
        os.environ["WIND_HUB_DEPLOYMENT_VERSION"] = case["deployment_version"]
        selection = select_local_service(
            root / "services" / case["service"], deployment_id=domain
        )
        loader = SharedDocumentLoader(
            SharedConfigPaths(
                instances=root / "instances",
                templates=root / "templates",
                site_file=root / "instances" / case["site"],
                devices_file=root / "instances" / case["devices"],
            )
        )
        documents = loader.load()
        snapshot = build_shared_snapshot(documents)
        point_counts = {
            str(key): len(value.points) for key, value in snapshot.point_tables.items()
        }
        detail = {
            "status": "PASS",
            "deployment_path": str(selection.deployment_path.relative_to(REPO)),
            "device_count": len(documents.devices),
            "device_ids": sorted(documents.devices),
            "business_point_catalog_version": documents.business_points.get("file_version"),
            "business_point_count": len(snapshot.business_points),
            "point_table_point_counts": point_counts,
            "source_manifest_count": len(snapshot.source_manifest),
        }
        if detail["device_count"] != case["expected_devices"]:
            failed.append(f"{domain}: device count")
        if point_counts != case["expected_points"]:
            failed.append(f"{domain}: point counts")
        if detail["business_point_catalog_version"] != "1.5.0":
            failed.append(f"{domain}: catalog version")
    except Exception as exc:
        detail = {"status": "REJECTED", "type": type(exc).__name__, "message": str(exc)}
        failed.append(f"{domain}: {exc}")
    results[domain] = detail

print(json.dumps(results, ensure_ascii=False, indent=2))
if failed:
    print("FAILURES:", json.dumps(failed, ensure_ascii=False))
    raise SystemExit(1)
