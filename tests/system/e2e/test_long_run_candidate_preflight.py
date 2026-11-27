"""Offline qualification of the exact E2E candidate before real smoke/soak.

This checks immutable references and transport contracts, not runtime health.
Long-run admission also requires real service, data and capacity smoke evidence.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from cli.runtime import load_config

ROOT = Path(__file__).resolve().parents[3]
DEPLOYMENT_NAME = "deployment_e2e-final_v2.0.2.yaml"
CLI_NAME = "service_cli-e2e_v1.1.9.yaml"
SERVICE_ROLES = {
    "gateway": "gateway-e2e-source",
    "gateway-target": "gateway-e2e-target",
    "data-generator": "data-generator-e2e",
    "collector-realtime": "collector-e2e-realtime",
    "collector-reliable": "collector-e2e-reliable",
    "commander": "commander-e2e",
    "sink": "sink-e2e",
    "storage": "storage-e2e",
}
COLLECTORS = frozenset(("collector", "collector-realtime", "collector-reliable"))
TOKEN_ROLES = frozenset(("gateway", "gateway-target", "data-generator", "commander"))
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")

pytestmark = pytest.mark.contract


def _document(path: Path) -> dict[str, object]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), path
    return doc


def test_active_immutable_e2e_candidate_is_exactly_pinned() -> None:
    """All declared Service/Instance references are exact and physically present."""
    source = ROOT / "configs"
    active = ROOT / "deploy" / "config_roots" / "e2e-final"
    deployment_source = source / "deployments" / DEPLOYMENT_NAME
    deployment_active = active / "deployments" / DEPLOYMENT_NAME
    cli_source = source / "services" / CLI_NAME
    cli_active = active / "services" / CLI_NAME

    assert deployment_source.read_bytes() == deployment_active.read_bytes()
    assert cli_source.read_bytes() == cli_active.read_bytes()
    deployment = _document(deployment_active)
    cli_document = _document(cli_active)
    assert deployment["deployment_id"] == "e2e-final"
    assert deployment["file_version"] == "2.0.2"
    assert cli_document["service_id"] == "cli-e2e"
    assert cli_document["file_version"] == "1.1.9"

    selected = deployment["services"]
    assert isinstance(selected, dict)
    assert selected["cli-e2e"] == {"config_id": "cli-e2e", "file_version": "1.1.9"}
    for service_id in SERVICE_ROLES.values():
        row = selected[service_id]
        version = row["file_version"]
        assert VERSION.fullmatch(version), (service_id, version)
        assert row["config_id"] == service_id
        name = f"service_{service_id}_v{version}.yaml"
        path = source / "services" / name
        assert path.is_file(), path
        doc = _document(path)
        assert doc["service_id"] == service_id
        assert doc["file_version"] == version
        assert doc["config_sources"].keys() == {"instances"}

    instances = deployment["instances"]
    assert isinstance(instances, dict)
    stems = {
        "site": "site",
        "device": "device",
        "collector_tasks": "collector_tasks",
        "scenario": "scenarios",
        "sink": "sinks",
        "delivery": "deliveries",
    }
    for kind, references in instances.items():
        assert kind in stems
        for identity, version_ref in references.items():
            version = version_ref["file_version"]
            assert VERSION.fullmatch(version)
            filename = f"{stems[kind]}_{identity}_v{version}.yaml"
            assert (source / "instances" / filename).is_file(), filename


def test_e2e_cli_processes_use_selected_versions_and_correct_auth() -> None:
    cli_path = ROOT / "deploy" / "config_roots" / "e2e-final" / "services" / CLI_NAME
    cli = load_config(cli_path)
    aliases = set(cli.services) - set(SERVICE_ROLES) - {"collector", "configuration"}
    assert not aliases, f"unqualified CLI targets: {sorted(aliases)}"
    assert cli.services["configuration"].manager == "process"
    assert cli.services["configuration"].tls_profile is None
    assert cli.services["collector"].tls_profile is None
    assert set(COLLECTORS) <= set(cli.services)
    assert not cli.profiles

    for name, service_id in SERVICE_ROLES.items():
        target = cli.services[name]
        assert target.manager == "process"
        assert target.process is not None
        assert target.grpc_endpoint is not None
        assert target.grpc_endpoint.startswith("127.0.0.1:")
        selected = _document(ROOT / "configs" / "deployments" / DEPLOYMENT_NAME)["services"][
            service_id
        ]
        version = selected["file_version"]
        if name != "storage":
            selected_file = f"service_{service_id}_v{version}.yaml"
            assert any(
                part == "configs/services/" + selected_file
                or part.endswith("/configs/services/" + selected_file)
                for part in target.process.argv
            ), name
        if name in COLLECTORS:
            assert target.tls_profile is None
            assert target.token_env is None
            # No certificate environment in the CLI process map means the
            # Collector must bind to literal loopback without mTLS.
            assert not {
                "WIND_HUB_COLLECTOR_TLS_CERT",
                "WIND_HUB_COLLECTOR_TLS_KEY",
                "WIND_HUB_COLLECTOR_TLS_CA",
            } & set(target.process.env)
        elif name in TOKEN_ROLES:
            assert target.tls_profile is None
            assert target.token_env is not None
        else:
            assert target.tls_profile is None

    commander = cli.services["commander"]
    assert dict(commander.method_token_envs) == {
        "RecoverDeviceControl": "WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN"
    }
    for token_role in TOKEN_ROLES:
        assert cli.services[token_role].token_env.startswith("WIND_HUB_E2E_")


def test_e2e_transport_endpoints_match_actual_service_bindings() -> None:
    """Service listener, CLI target and caller identity must form one closure."""
    root = ROOT / "configs"
    cli = load_config(ROOT / "deploy" / "config_roots" / "e2e-final" / "services" / CLI_NAME)
    selections = _document(root / "deployments" / DEPLOYMENT_NAME)["services"]
    for name, identity in SERVICE_ROLES.items():
        version = selections[identity]["file_version"]
        document = _document(root / "services" / f"service_{identity}_v{version}.yaml")
        target = cli.services[name]
        if name == "data-generator":
            endpoint = document["service"]["grpc_bind"]
        elif name == "storage":
            listener = document["service"]["grpc"]
            endpoint = f"{listener['listen_host']}:{listener['listen_port']}"
        elif name.startswith("collector-"):
            argv = target.process.argv
            port = argv[argv.index("--grpc-port") + 1]
            host = argv[argv.index("--grpc-host") + 1]
            endpoint = f"{host}:{port}"
        else:
            listener = document["grpc"]
            endpoint = f"{listener['listen_host']}:{listener['listen_port']}"
        assert target.grpc_endpoint == endpoint, (name, endpoint)

        if name not in TOKEN_ROLES:
            continue
        callers = document["security"]["callers"]
        expected_identity = {
            "gateway": "operator",
            "gateway-target": "operator",
            "data-generator": "cli",
            "commander": "ems",
        }[name]
        configured = {caller["caller_id"]: caller["token_env"] for caller in callers}
        assert target.token_env == configured[expected_identity], name
        if name == "commander":
            methods = dict(target.method_token_envs)
            assert methods["RecoverDeviceControl"] == configured["maintenance"]


def test_e2e_device_models_pin_existing_point_tables_and_catalogs() -> None:
    """Refuse unresolvable immutable DeviceModel -> PointTable -> catalog closures."""
    root = ROOT / "configs"
    devices = _document(root / "instances" / "device_e2e-final_v1.0.1.yaml")
    device_map = devices["devices"]
    assert isinstance(device_map, dict) and device_map
    checked_tables: set[str] = set()
    for device_id, entry in device_map.items():
        model_ref = entry["model"]
        model_path = (
            root
            / "templates"
            / "device_models"
            / f"device_model_{model_ref['id']}_v{model_ref['version']}.yaml"
        )
        assert model_path.is_file(), (device_id, model_path)
        model = _document(model_path)
        assert model["template_id"] == model_ref["id"]
        assert model["file_version"] == str(model_ref["version"])
        table_ref = model["point_table"]
        table_name = f"point_table_{table_ref['id']}_v{table_ref['version']}.yaml"
        table_path = root / "templates" / "point_tables" / table_name
        assert table_path.is_file(), (device_id, table_name)
        table = _document(table_path)
        assert table["template_id"] == table_ref["id"]
        assert table["file_version"] == str(table_ref["version"])
        checked_tables.add(table_name)

        for source in table.get("config_sources", []):
            source_type = source["config_type"]
            assert source_type in {"units", "business_points"}
            assert source["config_id"] == "standard"
            version = source["file_version"]
            catalog = root / "templates" / f"{source_type}_standard_v{version}.yaml"
            assert catalog.is_file(), (device_id, source_type, version)
            catalog_document = _document(catalog)
            assert catalog_document["file_version"] == str(version)
    assert len(checked_tables) >= 5
