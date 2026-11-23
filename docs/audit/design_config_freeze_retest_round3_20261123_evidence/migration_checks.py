from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO = Path(sys.argv[1]).resolve()
CONFIGS = REPO / "configs"


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def without(document, keys):
    return {key: value for key, value in document.items() if key not in keys}


results = {"point_tables": {}, "device_models": {}, "device_instances": {}, "other": {}}
failures = []
pt_ids = [
    "ads_wtg_4xems", "iec104_wtg", "modbus_bess_sim", "modbus_grid_sim",
    "modbus_plant_controller_sim", "modbus_pv_sim", "modbus_wtg_sim", "modbus_wtg_tz2mw",
]
for template_id in pt_ids:
    old_path = CONFIGS / f"templates/point_tables/point_table_{template_id}_v1.1.0.yaml"
    new_path = CONFIGS / f"templates/point_tables/point_table_{template_id}_v1.2.0.yaml"
    old = load(old_path)
    new = load(new_path)
    old_semantic = without(old, {"file_version", "config_sources"})
    new_semantic = without(new, {"file_version", "config_sources"})
    equal = old_semantic == new_semantic
    results["point_tables"][template_id] = {
        "old_new_semantics_equal_except_version_and_sources": equal,
        "point_count": len(new["points"]),
        "config_sources": new.get("config_sources"),
    }
    if not equal:
        failures.append(f"PointTable semantic drift: {template_id}")

model_ids = [
    "bess_sim_physical", "grid_sim_physical", "plant_controller_sim_physical",
    "pv_sim_physical", "wtg_sim_physical",
]
for model_id in model_ids:
    old = load(CONFIGS / f"templates/device_models/device_model_{model_id}_v1.1.0.yaml")
    new = load(CONFIGS / f"templates/device_models/device_model_{model_id}_v1.2.0.yaml")
    equal = without(old, {"file_version", "point_table"}) == without(new, {"file_version", "point_table"})
    point_table_change_only = old["point_table"]["id"] == new["point_table"]["id"] and new["point_table"]["version"] == "1.2.0"
    results["device_models"][model_id] = {"non_table_fields_equal": equal, "expected_table_change": point_table_change_only}
    if not equal or not point_table_change_only:
        failures.append(f"DeviceModel drift: {model_id}")

old_tz = load(CONFIGS / "templates/device_models/device_model_wtg_tz2mw_v1.3.0.yaml")
new_tz = load(CONFIGS / "templates/device_models/device_model_wtg_tz2mw_v1.4.0.yaml")
tz_equal = without(old_tz, {"file_version", "point_table"}) == without(new_tz, {"file_version", "point_table"})
results["device_models"]["wtg_tz2mw"] = {"non_table_fields_equal": tz_equal, "new_table": new_tz["point_table"]}
if not tz_equal or new_tz["point_table"] != {"id": "modbus_wtg_tz2mw", "version": "1.2.0"}:
    failures.append("TZ Model drift")

for name, old_version, new_version in [("sim", "1.2.0", "1.3.0"), ("e2e-final", "1.0.0", "1.0.1")]:
    old = load(CONFIGS / f"instances/device_{name}_v{old_version}.yaml")
    new = load(CONFIGS / f"instances/device_{name}_v{new_version}.yaml")
    non_devices_equal = without(old, {"file_version", "devices"}) == without(new, {"file_version", "devices"})
    changed = []
    for device_id in old["devices"]:
        before = old["devices"][device_id]
        after = new["devices"][device_id]
        if without(before, {"model"}) != without(after, {"model"}) or before["model"]["id"] != after["model"]["id"]:
            changed.append(device_id)
        elif after["model"]["version"] != "1.2.0":
            changed.append(device_id)
    results["device_instances"][name] = {"non_device_fields_equal": non_devices_equal, "unexpected_device_changes": changed}
    if not non_devices_equal or changed:
        failures.append(f"Device Instance drift: {name}")

old_farm = load(CONFIGS / "instances/device_farm-a_v1.5.0.yaml")
new_farm = load(CONFIGS / "instances/device_farm-a_v1.6.0.yaml")
farm_changes = []
for device_id in old_farm["devices"]:
    before = old_farm["devices"][device_id]
    after = new_farm["devices"][device_id]
    if without(before, {"model"}) != without(after, {"model"}) or before["model"]["id"] != after["model"]["id"]:
        farm_changes.append(device_id)
    expected = "1.5.0" if device_id.startswith("GW") else "1.4.0"
    if after["model"]["version"] != expected:
        farm_changes.append(device_id)
results["device_instances"]["farm-a"] = {"unexpected_device_changes": sorted(set(farm_changes))}
if farm_changes:
    failures.append("farm-a Device Instance drift")

old_delivery = load(CONFIGS / "instances/deliveries_prod-a_v1.4.0.yaml")
new_delivery = load(CONFIGS / "instances/deliveries_prod-a_v1.5.0.yaml")
collector_changes = {}
other_delivery_changes = {}
for delivery_id in old_delivery["deliveries"]:
    before = old_delivery["deliveries"][delivery_id]
    after = new_delivery["deliveries"][delivery_id]
    old_source = without(before["source"], {"collector_id"})
    new_source = without(after["source"], {"collector_id"})
    if without(before, {"source"}) != without(after, {"source"}) or old_source != new_source:
        other_delivery_changes[delivery_id] = True
    collector_changes[delivery_id] = {
        "old": before["source"]["collector_id"], "new": after["source"]["collector_id"]
    }
results["other"]["prod_delivery"] = {"collector_changes": collector_changes, "other_changes": other_delivery_changes}
if other_delivery_changes:
    failures.append("Unexpected prod Delivery drift")

old_storage = load(CONFIGS / "services/service_storage-main_v1.1.0.yaml")
new_storage = load(CONFIGS / "services/service_storage-main_v1.2.0.yaml")
old_without_kafka = old_storage.copy()
new_without_kafka = new_storage.copy()
old_without_kafka["service"] = without(old_storage["service"], {"kafka"})
new_without_kafka["service"] = without(new_storage["service"], {"kafka"})
storage_other_equal = without(old_without_kafka, {"file_version"}) == without(new_without_kafka, {"file_version"})
results["other"]["storage-main"] = {
    "non_kafka_fields_equal": storage_other_equal,
    "old_kafka": old_storage["service"]["kafka"],
    "new_kafka": new_storage["service"]["kafka"],
}
if not storage_other_equal:
    failures.append("Unexpected Storage drift")

summary = {"results": results, "failures": failures}
print(json.dumps(summary, ensure_ascii=False, indent=2))
raise SystemExit(1 if failures else 0)
