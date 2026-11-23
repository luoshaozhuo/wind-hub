from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

REPO = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
CONFIGS = REPO / "configs"
ROOTS = REPO / "deploy/config_roots"
EXPECTED_COUNTS = {"prod-a": 29, "sim-a": 23, "e2e-final": 35}
EXPECTED_DEPLOYMENTS = {"prod-a": "1.5.0", "sim-a": "1.2.0", "e2e-final": "1.0.1"}
DEVICE_FILES = {
    "prod-a": "device_farm-a_v1.6.0.yaml",
    "sim-a": "device_sim_v1.3.0.yaml",
    "e2e-final": "device_e2e-final_v1.0.1.yaml",
}


class StrictLoader(yaml.SafeLoader):
    pass


def strict_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, strict_mapping)


def load(path: Path):
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)
    if not isinstance(document, dict):
        raise ValueError(f"non-mapping root: {path}")
    version = document.get("file_version")
    if not isinstance(version, str) or not path.stem.endswith("_v" + version):
        raise ValueError(f"file version mismatch: {path}")
    if type(document.get("schema_version")) is not int:
        raise ValueError(f"invalid schema_version: {path}")
    return document


def sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_sources(document, path):
    expected = {
        ("units", "standard", "1.2.0"),
        ("business_points", "standard", "1.5.0"),
    }
    actual = {
        (item.get("config_type"), item.get("config_id"), item.get("file_version"))
        for item in document.get("config_sources", [])
    }
    if actual != expected:
        raise ValueError(f"PointTable source closure mismatch: {path}: {actual}")


results = {}
manifest_rows = []
failures = []
all_root_relative = {}
for domain in sorted(EXPECTED_COUNTS):
    root = ROOTS / domain
    paths = sorted(root.rglob("*.yaml"))
    documents = {path: load(path) for path in paths}
    relative_set = {path.relative_to(root).as_posix() for path in paths}
    all_root_relative[domain] = relative_set
    for path in paths:
        relative = path.relative_to(root)
        source = CONFIGS / relative
        match = source.is_file() and source.read_bytes() == path.read_bytes()
        manifest_rows.append(
            {
                "deployment": domain,
                "root_relative_path": relative.as_posix(),
                "source_path": source.relative_to(REPO).as_posix(),
                "sha256": sha256(path),
                "source_bytes_equal": str(match).lower(),
            }
        )
        if not match:
            failures.append(f"{domain}:{relative}: source hash mismatch")
    deployments = [path for path in paths if path.parent == root / "deployments"]
    if len(paths) != EXPECTED_COUNTS[domain]:
        failures.append(f"{domain}: YAML count {len(paths)}")
    if len(deployments) != 1:
        failures.append(f"{domain}: Deployment count {len(deployments)}")
        continue
    deployment_path = deployments[0]
    deployment = documents[deployment_path]
    if deployment["file_version"] != EXPECTED_DEPLOYMENTS[domain]:
        failures.append(f"{domain}: Deployment version")
    if deployment.get("deployment_id") != domain:
        failures.append(f"{domain}: Deployment identity")

    selected_services = {}
    for service_id, selection in deployment["services"].items():
        path = root / "services" / f"service_{service_id}_v{selection['file_version']}.yaml"
        if path not in documents:
            failures.append(f"{domain}: missing service {service_id}")
            continue
        selected_services[service_id] = documents[path]
    instance_paths = {}
    prefixes = {
        "site": "site", "device": "device", "collector_tasks": "collector_tasks",
        "scenario": "scenarios", "sink": "sinks", "delivery": "deliveries",
    }
    for kind, entries in deployment["instances"].items():
        for instance_id, selection in entries.items():
            path = root / "instances" / f"{prefixes[kind]}_{instance_id}_v{selection['file_version']}.yaml"
            if path not in documents:
                failures.append(f"{domain}: missing instance {kind}/{instance_id}")
            else:
                instance_paths[kind, instance_id] = path

    for service_id, service in selected_services.items():
        for role, instance_id in service.get("config_sources", {}).get("instances", {}).items():
            kind = {
                "site": "site", "devices": "device", "tasks": "collector_tasks",
                "scenarios": "scenario", "sinks": "sink", "deliveries": "delivery",
            }[role]
            if (kind, instance_id) not in instance_paths:
                failures.append(f"{domain}: {service_id} unselected {role}/{instance_id}")

    table_points = {}
    for path in paths:
        if path.parent == root / "templates/point_tables":
            document = documents[path]
            check_sources(document, path)
            table_points[path] = document["points"]
    bp_path = root / "templates/business_points_standard_v1.5.0.yaml"
    bp_document = documents[bp_path]
    bp_sources = {
        (item.get("config_type"), item.get("config_id"), item.get("file_version"))
        for item in bp_document.get("config_sources", [])
    }
    if bp_sources != {("units", "standard", "1.2.0")}:
        failures.append(f"{domain}: BusinessPoint Units pin")

    device_path = root / "instances" / DEVICE_FILES[domain]
    device_document = documents[device_path]
    device_points = {}
    for device_id, device in device_document["devices"].items():
        model = device["model"]
        model_path = root / "templates/device_models" / f"device_model_{model['id']}_v{model['version']}.yaml"
        if model_path not in documents:
            failures.append(f"{domain}:{device_id}: missing model")
            continue
        model_document = documents[model_path]
        table = model_document["point_table"]
        table_path = root / "templates/point_tables" / f"point_table_{table['id']}_v{table['version']}.yaml"
        if table_path not in documents:
            failures.append(f"{domain}:{device_id}: missing PointTable")
            continue
        device_points[device_id] = set(table_points[table_path])

    collector_ids = {service_id for service_id in selected_services if service_id.startswith("collector-")}
    delivery_count = 0
    source_pair_count = 0
    for (kind, _), path in instance_paths.items():
        if kind != "delivery":
            continue
        for delivery_id, delivery in documents[path]["deliveries"].items():
            delivery_count += 1
            source = delivery["source"]
            if source["collector_id"] not in collector_ids:
                failures.append(f"{domain}:{delivery_id}: collector identity {source['collector_id']}")
            for device_id in source["devices"]:
                for point_id in source["points"]:
                    source_pair_count += 1
                    if point_id not in device_points.get(device_id, set()):
                        failures.append(f"{domain}:{delivery_id}: missing source {device_id}/{point_id}")

    storage_topics = {}
    for service_id, service in selected_services.items():
        if service_id.startswith("storage-"):
            storage_topics[service_id] = {
                "topic": service["service"]["kafka"]["topic"],
                "bootstrap_servers": service["service"]["kafka"]["bootstrap_servers"],
            }
    kafka_targets = {}
    for (kind, _), path in instance_paths.items():
        if kind != "sink":
            continue
        for sink_id, sink in documents[path]["sinks"].items():
            template_path = root / "templates/sinks" / f"sink_{sink['template_id']}_v{sink['template_version']}.yaml"
            template = documents[template_path]
            if template["kind"] == "kafka":
                kafka_targets[sink_id] = {
                    "topic": template["output"]["topic"],
                    "bootstrap_servers": sink.get("connection", {}).get(
                        "bootstrap_servers", template["connection"]["bootstrap_servers"]
                    ),
                }
    if domain == "prod-a":
        storage = storage_topics.get("storage-main")
        sink = kafka_targets.get("history_kafka")
        if not storage or not sink:
            failures.append("prod-a: missing Storage/Kafka target")
        else:
            if storage["topic"] != sink["topic"]:
                failures.append("prod-a: Kafka topic mismatch")
            if [storage["bootstrap_servers"]] != sink["bootstrap_servers"] and storage["bootstrap_servers"] != sink["bootstrap_servers"]:
                failures.append("prod-a: Kafka broker mismatch")
    if domain == "e2e-final":
        storage = storage_topics.get("storage-e2e")
        sink = kafka_targets.get("e2e_kafka")
        if not storage or not sink or storage["topic"] != sink["topic"]:
            failures.append("e2e-final: Kafka topic mismatch")

    results[domain] = {
        "yaml_count": len(paths),
        "deployment": f"{domain}@{deployment['file_version']}",
        "service_count": len(selected_services),
        "instance_count": len(instance_paths),
        "point_tables": {path.stem: len(points) for path, points in table_points.items()},
        "device_count": len(device_points),
        "delivery_count": delivery_count,
        "source_device_point_pairs": source_pair_count,
        "collector_ids": sorted(collector_ids),
        "kafka_targets": kafka_targets,
        "storage_targets": storage_topics,
        "all_root_files_match_source": all(
            row["source_bytes_equal"] == "true" for row in manifest_rows if row["deployment"] == domain
        ),
    }

shared_paths = set.intersection(*(set(value) for value in all_root_relative.values()))
# Shared immutable Templates are allowed; Service/Instance/Deployment documents are domain-specific.
unexpected_shared = sorted(
    path for path in shared_paths if not path.startswith("templates/")
)
if unexpected_shared:
    failures.append(f"unexpected shared domain Service/Instance/Deployment paths: {unexpected_shared}")

sim_cli = load(ROOTS / "sim-a/services/service_cli-sim_v1.3.0.yaml")
sim_cli_text = (ROOTS / "sim-a/services/service_cli-sim_v1.3.0.yaml").read_text(encoding="utf-8")
sim_cli_forbidden = [token for token in ["prod-a", "service_data-generator_v1.1.0", "storage_bootstrap"] if token in sim_cli_text]
if sim_cli_forbidden:
    failures.append(f"sim CLI production targets: {sim_cli_forbidden}")
results["sim-a"]["cli_forbidden_target_tokens"] = sim_cli_forbidden

# Strictly parse the complete versioned source repository as well.
config_paths = sorted(CONFIGS.rglob("*.yaml"))
for path in config_paths:
    load(path)
schema_counts = Counter(load(path)["schema_version"] for path in config_paths)

changed = subprocess.check_output(
    ["git", "diff", "--name-status", "227323ac368e4090d9884e7da4a1b7d2c6fadc9e..HEAD", "--", "configs"],
    cwd=REPO,
    text=True,
).splitlines()
modified_existing = [line for line in changed if line.startswith("M\t")]
results["source_configs"] = {
    "yaml_count": len(config_paths),
    "schema_versions": dict(sorted(schema_counts.items())),
    "modified_existing_since_round_1": modified_existing,
    "added_paths": sum(line.startswith("A\t") for line in changed),
}
if modified_existing:
    failures.append(f"same-version existing YAML modified: {modified_existing}")

summary = {"results": results, "unexpected_shared_domain_documents": unexpected_shared, "failures": failures}
(OUT / "static-root-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
with (OUT / "root-source-hash-manifest.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(manifest_rows[0]))
    writer.writeheader()
    writer.writerows(manifest_rows)
print(json.dumps(summary, ensure_ascii=False, indent=2))
raise SystemExit(1 if failures else 0)
