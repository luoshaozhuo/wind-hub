from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(REPO / "src"))

from commander.infrastructure.config.service_settings import validate_transport_settings
from configuration_service.application.schema_catalog import iter_schemas
from configuration_service.application.service import ConfigurationApplication
from configuration_service.settings import load_settings as load_configuration_bootstrap
from core.configuration.publication_identity import resolve_publication_identity
from data_generator.infrastructure.config.service_settings import load_settings as load_generator_settings
from industrial_gateway.infrastructure.config.schema import GatewayServiceConfig

schemas = {(item.config_type, item.schema_version): item for item in iter_schemas()}
results = {"repository_schema": {}, "sim_bootstrap": {}, "sim_runtime_parsers": {}}
failures = []
for domain in ["prod-a", "sim-a", "e2e-final"]:
    root = REPO / "deploy/config_roots" / domain
    domain_result = {"passed": [], "failed": {}}
    for path in sorted(root.rglob("*.yaml")):
        relative = path.relative_to(root).as_posix()
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            identity = resolve_publication_identity(relative, document)
            schema = schemas[identity.config_type, document["schema_version"]]
            ConfigurationApplication._validate_schema(document, schema.fields)
            domain_result["passed"].append(relative)
        except Exception as exc:
            domain_result["failed"][relative] = f"{type(exc).__name__}: {exc}"
    results["repository_schema"][domain] = {
        "pass_count": len(domain_result["passed"]),
        "fail_count": len(domain_result["failed"]),
        "failures": domain_result["failed"],
    }
    if domain_result["failed"]:
        failures.append(f"{domain}: repository schema failures")

bootstrap_path = REPO / "deploy/sim-a/configuration_bootstrap_sim-a_v1.0.0.yaml"
try:
    settings = load_configuration_bootstrap(bootstrap_path)
    results["sim_bootstrap"] = {
        "status": "PASS", "service_id": settings.service_id,
        "host": settings.host, "port": settings.port,
        "token_identities": sorted(settings.token_sources),
    }
except Exception as exc:
    results["sim_bootstrap"] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    failures.append("sim bootstrap")

runtime_cases = {
    "commander-sim@1.2.0": (
        REPO / "deploy/config_roots/sim-a/services/service_commander-sim_v1.2.0.yaml",
        validate_transport_settings,
    ),
    "gateway-sim@1.2.0": (
        REPO / "deploy/config_roots/sim-a/services/service_gateway-sim_v1.2.0.yaml",
        GatewayServiceConfig,
    ),
    "data-generator-sim@1.1.0": (
        REPO / "deploy/config_roots/sim-a/services/service_data-generator-sim_v1.1.0.yaml",
        load_generator_settings,
    ),
}
for name, (path, parser) in runtime_cases.items():
    try:
        if parser is validate_transport_settings:
            parser(yaml.safe_load(path.read_text(encoding="utf-8")))
        elif parser is GatewayServiceConfig:
            parser(**yaml.safe_load(path.read_text(encoding="utf-8")))
        else:
            parser(path)
        results["sim_runtime_parsers"][name] = {"status": "PASS"}
    except Exception as exc:
        results["sim_runtime_parsers"][name] = {
            "status": "REJECTED",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }

print(json.dumps(results, ensure_ascii=False, indent=2))
# Runtime parser rejections are recorded as code-review findings, not command failure.
raise SystemExit(1 if failures else 0)
