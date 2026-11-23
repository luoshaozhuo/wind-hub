from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
CONFIGS = ROOT / "configs"
TEMPLATES = CONFIGS / "templates"


class StrictLoader(yaml.SafeLoader):
    pass


def strict_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, strict_mapping
)


def load(path: Path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)


def sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


bp14_path = TEMPLATES / "business_points_standard_v1.4.0.yaml"
bp15_path = TEMPLATES / "business_points_standard_v1.5.0.yaml"
pt11_path = TEMPLATES / "point_tables/point_table_ads_wtg_gw2mw_v1.1.0.yaml"
pt12_path = TEMPLATES / "point_tables/point_table_ads_wtg_gw2mw_v1.2.0.yaml"
units_path = TEMPLATES / "units_standard_v1.2.0.yaml"
mapping_path = ROOT / "docs/audit/gw2mw_businesspoint_semantic_mapping_candidate_20261123.csv"

bp14_doc = load(bp14_path)
bp15_doc = load(bp15_path)
pt11_doc = load(pt11_path)
pt12_doc = load(pt12_path)
units_doc = load(units_path)
bp14 = bp14_doc["business_points"]
bp15 = bp15_doc["business_points"]
pt11 = pt11_doc["points"]
pt12 = pt12_doc["points"]
units = units_doc["units"]
with mapping_path.open(encoding="utf-8", newline="") as handle:
    mapping = list(csv.DictReader(handle))

original_ids = set(bp14)
new_ids = set(bp15)
added_ids = sorted(new_ids - original_ids)
removed_ids = sorted(original_ids - new_ids)
changed_original = sorted(key for key in original_ids & new_ids if bp14[key] != bp15[key])

catalog_rows = []
for key in sorted(original_ids | new_ids):
    if key in removed_ids:
        status = "FAIL_REMOVED_ORIGINAL"
    elif key in added_ids:
        definition = bp15[key]
        complete = (
            isinstance(definition, dict)
            and isinstance(definition.get("data_type"), str)
            and bool(definition["data_type"])
            and isinstance(definition.get("unit"), str)
            and definition["unit"] in units
            and isinstance(definition.get("description"), str)
            and bool(definition["description"].strip())
        )
        status = "PASS_COMPLETE_CANDIDATE" if complete else "FAIL_INCOMPLETE_CANDIDATE"
    elif bp14[key] == bp15[key]:
        status = "PASS_UNCHANGED"
    else:
        status = "FAIL_CHANGED_ORIGINAL"
    before = bp14.get(key, {})
    after = bp15.get(key, {})
    catalog_rows.append(
        {
            "business_point_id": key,
            "membership": "original" if key in original_ids else "added",
            "old_data_type": before.get("data_type", ""),
            "new_data_type": after.get("data_type", ""),
            "old_unit": before.get("unit", ""),
            "new_unit": after.get("unit", ""),
            "definition_equal": str(before == after).lower(),
            "status": status,
        }
    )

catalog_output = OUT / "businesspoint_catalog_diff_20261123.csv"
with catalog_output.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(catalog_rows[0]))
    writer.writeheader()
    writer.writerows(catalog_rows)

mapping_by_point = {}
mapping_duplicate_points = []
for row in mapping:
    point_id = row["point_id"]
    if point_id in mapping_by_point:
        mapping_duplicate_points.append(point_id)
    mapping_by_point[point_id] = row

allowed_source_to_business = {
    ("BOOL", "bool"),
    ("REAL", "float32"),
    ("DINT", "int32"),
    ("INT", "int32"),
    ("UINT", "int32"),
    ("UDINT", "int32"),
    ("LREAL", "float64"),
    ("STRING", "string"),
}

point_ids = set(pt11) | set(pt12) | set(mapping_by_point)
point_rows = []
for point_id in sorted(point_ids):
    old = pt11.get(point_id)
    new = pt12.get(point_id)
    row = mapping_by_point.get(point_id)
    checks = {}
    checks["point_present_in_old_new_csv"] = old is not None and new is not None and row is not None
    if old is None or new is None or row is None:
        point_rows.append(
            {
                "point_id": point_id,
                "old_business_point_id": (old or {}).get("business_point_id", ""),
                "new_business_point_id": (new or {}).get("business_point_id", ""),
                "ads_symbol": ((new or old or {}).get("address") or {}).get("symbol", ""),
                "checks": "point_present_in_old_new_csv=FAIL",
                "oem_status": (row or {}).get("verification", "MISSING_CSV"),
                "structural_status": "FAIL",
            }
        )
        continue
    old_nonsemantic = {k: v for k, v in old.items() if k not in {"business_point_id", "description"}}
    new_nonsemantic = {k: v for k, v in new.items() if k not in {"business_point_id", "description"}}
    proposed = new.get("business_point_id")
    business = bp15.get(proposed)
    source_type = new.get("data_type")
    source_unit = new.get("unit")
    standard_type = (business or {}).get("data_type")
    standard_unit = (business or {}).get("unit")
    source_quantity = (units.get(source_unit) or {}).get("quantity")
    standard_quantity = (units.get(standard_unit) or {}).get("quantity")
    checks.update(
        {
            "old_business_id_csv_match": row["old_business_point_id"] == old.get("business_point_id"),
            "new_business_id_csv_match": row["proposed_business_point_id"] == proposed,
            "old_business_id_exists_in_v140": old.get("business_point_id") in bp14,
            "new_business_id_exists_in_v150": proposed in bp15,
            "nonsemantic_protocol_fields_unchanged": old_nonsemantic == new_nonsemantic,
            "ads_symbol_csv_match": row["ads_symbol"] == (new.get("address") or {}).get("symbol"),
            "source_type_csv_match": row["source_data_type"] == source_type,
            "source_unit_csv_match": row["source_unit"] == source_unit,
            "standard_type_csv_match": row["standard_data_type"] == standard_type,
            "standard_unit_csv_match": row["standard_unit"] == standard_unit,
            "source_standard_type_compatible": (source_type, standard_type) in allowed_source_to_business,
            "source_standard_quantity_compatible": (
                source_quantity is not None and source_quantity == standard_quantity
            ),
            "identity_scaling_for_bool_or_string": (
                standard_type not in {"bool", "string"}
                or (new.get("scale", 1) == 1 and new.get("offset", 0) == 0)
            ),
            "candidate_description_retained": "候选" in new.get("description", "")
            and "待现场核实" in new.get("description", ""),
            "csv_pending_oem": row["verification"] == "PENDING_OEM_SEMANTICS",
        }
    )
    failed = [name for name, passed in checks.items() if not passed]
    point_rows.append(
        {
            "point_id": point_id,
            "old_business_point_id": old.get("business_point_id", ""),
            "new_business_point_id": proposed or "",
            "ads_symbol": (new.get("address") or {}).get("symbol", ""),
            "source_data_type": source_type,
            "source_unit": source_unit,
            "standard_data_type": standard_type or "",
            "standard_unit": standard_unit or "",
            "scale": new.get("scale", ""),
            "offset": new.get("offset", ""),
            "checks": ";".join(f"{name}={'PASS' if passed else 'FAIL'}" for name, passed in checks.items()),
            "failed_checks": ";".join(failed),
            "oem_status": row["verification"],
            "structural_status": "PASS" if not failed else "FAIL",
        }
    )

new_business_ids = [point.get("business_point_id") for point in pt12.values()]
business_counts = Counter(new_business_ids)
for row in point_rows:
    business_id = row["new_business_point_id"]
    unique = business_id and business_counts[business_id] == 1
    row["checks"] += f";new_business_id_unique={'PASS' if unique else 'FAIL'}"
    if not unique:
        row["structural_status"] = "FAIL"
        row["failed_checks"] = ";".join(filter(None, [row["failed_checks"], "new_business_id_unique"]))

point_output = OUT / "gw2mw_businesspoint_retest_results_20261123.csv"
fieldnames = [
    "point_id", "old_business_point_id", "new_business_point_id", "ads_symbol",
    "source_data_type", "source_unit", "standard_data_type", "standard_unit",
    "scale", "offset", "structural_status", "oem_status", "failed_checks", "checks",
]
with point_output.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(point_rows)

model14_path = TEMPLATES / "device_models/device_model_wtg_gw2mw_v1.4.0.yaml"
model15_path = TEMPLATES / "device_models/device_model_wtg_gw2mw_v1.5.0.yaml"
device14_path = CONFIGS / "instances/device_farm-a_v1.4.0.yaml"
device15_path = CONFIGS / "instances/device_farm-a_v1.5.0.yaml"
deployment13_path = CONFIGS / "deployments/deployment_prod-a_v1.3.0.yaml"
deployment14_path = CONFIGS / "deployments/deployment_prod-a_v1.4.0.yaml"
delivery13_path = CONFIGS / "instances/deliveries_prod-a_v1.3.0.yaml"
delivery14_path = CONFIGS / "instances/deliveries_prod-a_v1.4.0.yaml"
model14 = load(model14_path)
model15 = load(model15_path)
device14 = load(device14_path)
device15 = load(device15_path)
deployment13 = load(deployment13_path)
deployment14 = load(deployment14_path)
delivery13 = load(delivery13_path)
delivery14 = load(delivery14_path)

def normalized(document, *excluded):
    return {key: value for key, value in document.items() if key not in excluded}

model_nonversion_equal = normalized(model14, "file_version", "point_table") == normalized(model15, "file_version", "point_table")
device_nonversion_equal = normalized(device14, "file_version", "devices", "revision") == normalized(device15, "file_version", "devices", "revision")
device_changes = []
for device_id in sorted(set(device14["devices"]) | set(device15["devices"])):
    before = device14["devices"].get(device_id)
    after = device15["devices"].get(device_id)
    if before is None or after is None:
        device_changes.append(device_id)
    else:
        before_normalized = {k: v for k, v in before.items() if k != "model"}
        after_normalized = {k: v for k, v in after.items() if k != "model"}
        if before_normalized != after_normalized:
            device_changes.append(device_id)

delivery_semantic13 = normalized(delivery13, "file_version", "revision")
delivery_semantic14 = normalized(delivery14, "file_version", "revision")

summary = {
    "catalog": {
        "v1_4_0_count": len(bp14),
        "v1_5_0_count": len(bp15),
        "original_count": len(original_ids),
        "added_count": len(added_ids),
        "removed_ids": removed_ids,
        "changed_original": changed_original,
        "added_complete_count": sum(row["status"] == "PASS_COMPLETE_CANDIDATE" for row in catalog_rows),
        "original_unchanged_count": sum(row["status"] == "PASS_UNCHANGED" for row in catalog_rows),
    },
    "points": {
        "old_count": len(pt11),
        "new_count": len(pt12),
        "csv_count": len(mapping),
        "csv_duplicate_point_ids": mapping_duplicate_points,
        "unique_new_business_point_count": len(business_counts),
        "duplicate_new_business_point_ids": sorted(key for key, count in business_counts.items() if count != 1),
        "structural_pass_count": sum(row["structural_status"] == "PASS" for row in point_rows),
        "structural_fail_point_ids": [row["point_id"] for row in point_rows if row["structural_status"] != "PASS"],
        "pending_oem_count": sum(row["oem_status"] == "PENDING_OEM_SEMANTICS" for row in point_rows),
        "source_types": dict(sorted(Counter(point.get("data_type") for point in pt12.values()).items())),
    },
    "explicit_sources": {
        "point_table_v1_2_0": pt12_doc.get("config_sources"),
        "business_points_v1_5_0": bp15_doc.get("config_sources"),
        "historical_point_table_v1_1_0": pt11_doc.get("config_sources"),
    },
    "chain": {
        "model_v1_5_0_point_table": model15.get("point_table"),
        "model_v1_5_0_protocol": model15.get("protocol"),
        "model_nonversion_fields_equal_v1_4_0": model_nonversion_equal,
        "device_model_versions": sorted({device["model"]["version"] for device in device15["devices"].values()}),
        "device_ids": sorted(device15["devices"]),
        "device_nonmodel_change_ids": device_changes,
        "device_nonversion_equal_excluding_devices": device_nonversion_equal,
        "deployment_v1_4_0_selected_device": deployment14.get("instances", {}).get("device"),
        "deployment_v1_4_0_selected_delivery": deployment14.get("instances", {}).get("delivery"),
        "deployment_v1_4_0_storage_service_selected": "storage-main" in deployment14.get("services", {}),
        "delivery_semantics_equal_v1_3_0": delivery_semantic13 == delivery_semantic14,
        "deployment_service_changes": {
            key: {"old": deployment13.get("services", {}).get(key), "new": deployment14.get("services", {}).get(key)}
            for key in sorted(set(deployment13.get("services", {})) | set(deployment14.get("services", {})))
            if deployment13.get("services", {}).get(key) != deployment14.get("services", {}).get(key)
        },
        "deployment_instance_changes": {
            f"{kind}/{key}": {
                "old": deployment13.get("instances", {}).get(kind, {}).get(key),
                "new": deployment14.get("instances", {}).get(kind, {}).get(key),
            }
            for kind in sorted(set(deployment13.get("instances", {})) | set(deployment14.get("instances", {})))
            for key in sorted(set(deployment13.get("instances", {}).get(kind, {})) | set(deployment14.get("instances", {}).get(kind, {})))
            if deployment13.get("instances", {}).get(kind, {}).get(key) != deployment14.get("instances", {}).get(kind, {}).get(key)
        },
    },
    "sha256": {
        str(path.relative_to(ROOT)): sha256(path)
        for path in [bp14_path, bp15_path, pt11_path, pt12_path, units_path, mapping_path]
    },
    "outputs": [str(catalog_output), str(point_output)],
}

summary_output = OUT / "gw2mw_retest_summary.json"
summary_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
failed = (
    len(bp14) != 293
    or len(bp15) != 400
    or len(added_ids) != 107
    or removed_ids
    or changed_original
    or len(pt11) != 111
    or len(pt12) != 111
    or len(mapping) != 111
    or mapping_duplicate_points
    or len(business_counts) != 111
    or any(row["structural_status"] != "PASS" for row in point_rows)
    or any(row["oem_status"] != "PENDING_OEM_SEMANTICS" for row in point_rows)
)
sys.exit(1 if failed else 0)
