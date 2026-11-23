from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO = Path(sys.argv[1]).resolve()
ROOT = REPO / "deploy/config_roots/sim-a"
PROD = REPO / "deploy/config_roots/prod-a"


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


cli = load(ROOT / "services/service_cli-sim_v1.5.0.yaml")
prod_cli = load(PROD / "services/service_cli_v1.2.0.yaml")
expected = {
    "configuration": ("127.0.0.1:53060", "configuration-sim"),
    "collector": ("127.0.0.1:53051", "collector-sim"),
    "commander": ("127.0.0.1:53052", "commander-sim"),
    "gateway": ("127.0.0.1:53073", "gateway-sim"),
    "data-generator": ("127.0.0.1:53061", "data-generator-sim"),
}
failures = []
details = {}
for service_id, (endpoint, target) in expected.items():
    actual = cli["services"][service_id]
    process = actual.get("process", {})
    detail = {
        "endpoint": actual.get("grpc_endpoint"),
        "target": actual.get("target"),
        "manager": actual.get("manager"),
        "state_dir": process.get("state_dir"),
        "log_file": process.get("log_file"),
    }
    details[service_id] = detail
    if actual.get("grpc_endpoint") != endpoint:
        failures.append(f"{service_id}: endpoint")
    if actual.get("target") != target or actual.get("manager") != "process":
        failures.append(f"{service_id}: target/manager")
    if process.get("state_dir") != "/var/lib/wind-hub/sim-a/cli-processes":
        failures.append(f"{service_id}: state_dir")
    if not process.get("log_file", "").startswith("/var/lib/wind-hub/sim-a/logs/"):
        failures.append(f"{service_id}: log_file")

cli_text = (ROOT / "services/service_cli-sim_v1.5.0.yaml").read_text(encoding="utf-8")
tls_fields = [field for field in ["tls_profiles", "tls_profile", "ca_cert", "client_cert", "client_key"] if field in cli_text]
if tls_fields:
    failures.append(f"CLI TLS fields: {tls_fields}")

sim_endpoints = {item["grpc_endpoint"] for item in cli["services"].values()}
prod_endpoints = {item["grpc_endpoint"] for item in prod_cli["services"].values()}
sim_targets = {item["target"] for item in cli["services"].values()}
prod_targets = {item["target"] for item in prod_cli["services"].values()}
sim_states = {item["process"]["state_dir"] for item in cli["services"].values()}
prod_states = {item["process"]["state_dir"] for item in prod_cli["services"].values() if "process" in item}
overlap = {
    "endpoints": sorted(sim_endpoints & prod_endpoints),
    "targets": sorted(sim_targets & prod_targets),
    "state_dirs": sorted(sim_states & prod_states),
}
if any(overlap.values()):
    failures.append(f"production overlap: {overlap}")

commander = load(ROOT / "services/service_commander-sim_v1.2.0.yaml")
gateway = load(ROOT / "services/service_gateway-sim_v1.2.0.yaml")
generator = load(ROOT / "services/service_data-generator-sim_v1.1.0.yaml")
listeners = {
    "commander": commander["grpc"]["listen_port"],
    "gateway": gateway["grpc"]["listen_port"],
    "generator": generator["service"]["grpc_bind"],
}
if listeners != {"commander": 53052, "gateway": 53073, "generator": "127.0.0.1:53061"}:
    failures.append(f"listener mismatch: {listeners}")
for name, service in [("commander", commander), ("gateway", gateway), ("generator", generator)]:
    if service["schema_version"] != 2:
        failures.append(f"{name}: schema_version")
    for caller in service["security"]["callers"]:
        if set(caller) != {"caller_id", "token_env", "permissions"}:
            failures.append(f"{name}: caller fields {set(caller)}")

scenarios = load(ROOT / "instances/scenarios_sim_v1.0.4.yaml")["scenarios"]
scenario = next(item for item in scenarios if item["scenario_id"] == "wind-plant-physical")
scenario_gateways = {item["gateway_id"]: item["endpoint"] for item in scenario["gateways"]}
expected_gateway_ids = {item["server_id"] for item in gateway["servers"]}
if set(scenario_gateways) != expected_gateway_ids or set(scenario_gateways.values()) != {"127.0.0.1:53073"}:
    failures.append("scenario gateway routes")
control = next(item for item in gateway["servers"] if item["server_id"] == "gw-ppc-sim01")["control"]
if control["target"]["endpoint"] != "127.0.0.1:53052" or control["simulation"]["endpoint"] != "127.0.0.1:53061" or control["simulation"]["scenario_id"] != scenario["scenario_id"]:
    failures.append("PPC control/simulation route")

result = {
    "status": "PASS" if not failures else "FAIL",
    "cli_services": details,
    "production_overlap": overlap,
    "cli_tls_fields": tls_fields,
    "service_listeners": listeners,
    "scenario_gateway_count": len(scenario_gateways),
    "scenario_variable_mapping_changes": [
        {"gateway_id": gateway_id, "source": source, "target": target}
        for gateway in scenarios[0]["gateways"]
        for gateway_id in [gateway["gateway_id"]]
        for source, target in gateway.get("variable_mapping", {}).items()
        if source != target
    ],
    "failures": failures,
}
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(1 if failures else 0)
