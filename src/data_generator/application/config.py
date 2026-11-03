"""Validate Data Generator scenarios independently of Core's shared device catalog."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from data_generator.domain.model import GeneratorError, OutputMapping, OutputTarget


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    version: str
    description: str
    source: dict[str, Any]
    devices: tuple[str, ...]
    points: dict[tuple[str, str], dict[str, Any]]
    mappings: tuple[OutputMapping, ...]
    gateways: dict[str, str]


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    service: dict[str, Any]
    scenarios: dict[str, Scenario]
    revision: str


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GeneratorError(f"{path}: YAML mapping expected")
    if data.get("schema_version") != 1:
        raise GeneratorError(f"{path}: unsupported schema version")
    version = data.get("file_version")
    if not isinstance(version, str) or len(version.split(".")) != 3 or any(
        not part.isdigit() for part in version.split(".")
    ):
        raise GeneratorError(f"{path}: expected MAJOR.MINOR.PATCH file_version")
    return data


def resolve_history(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise GeneratorError("history path must be relative")
    base = (root / "history").resolve()
    candidate = (base / relative).resolve()
    if not candidate.is_relative_to(base):
        raise GeneratorError("history path escapes allowed directory")
    return candidate


def load_settings(service_path: Path) -> Settings:
    path = service_path.resolve(strict=True)
    if path.parent.name != "services":
        raise GeneratorError("service configuration must be inside services/")
    root = path.parent.parent
    service = load_yaml(path)
    instance = load_yaml(root / "instances" / "data-generator.yaml")
    raw_scenarios = instance.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise GeneratorError("at least one scenario required")
    scenarios: dict[str, Scenario] = {}
    for raw in raw_scenarios:
        if not isinstance(raw, dict):
            raise GeneratorError("invalid scenario")
        scenario_id, version = raw.get("scenario_id"), raw.get("version")
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in scenarios:
            raise GeneratorError("duplicate or invalid scenario ID")
        if not isinstance(version, str) or not version:
            raise GeneratorError("invalid scenario version")
        source = raw.get("source")
        if not isinstance(source, dict) or source.get("kind") not in ("simulation", "replay"):
            raise GeneratorError("invalid scenario source")
        points: dict[tuple[str, str], dict[str, Any]] = {}
        device_ids: list[str] = []
        for device in raw.get("devices", []):
            device_id = device.get("device_id")
            if not isinstance(device_id, str) or not device_id or device_id in device_ids:
                raise GeneratorError("duplicate or invalid device")
            device_ids.append(device_id)
            for point in device.get("points", []):
                key = (device_id, point.get("point_id"))
                if key[1] is None or key in points:
                    raise GeneratorError("duplicate or invalid point")
                points[key] = point
        if not points:
            raise GeneratorError("scenario must define points")
        gateways: dict[str, str] = {}
        mappings: list[OutputMapping] = []
        targets: set[OutputTarget] = set()
        for gateway in raw.get("gateways", []):
            gateway_id, endpoint = gateway.get("gateway_id"), gateway.get("endpoint")
            if not isinstance(gateway_id, str) or not gateway_id or gateway_id in gateways:
                raise GeneratorError("duplicate gateway")
            if not isinstance(endpoint, str) or not endpoint:
                raise GeneratorError("missing gateway endpoint")
            gateways[gateway_id] = endpoint
            for mapping in gateway.get("feedback_mapping", []):
                key = (mapping.get("device_id"), mapping.get("point_id"))
                if key not in points:
                    raise GeneratorError("mapping references unknown point")
                target = OutputTarget(gateway_id, mapping.get("variable_id", ""))
                if target in targets:
                    raise GeneratorError("duplicate output target")
                targets.add(target)
                mappings.append(OutputMapping(key[0], key[1], target))
        if set(points) - {(m.device_id, m.point_id) for m in mappings}:
            raise GeneratorError("enabled point has no output mapping")
        scenarios[scenario_id] = Scenario(
            scenario_id, version, str(raw.get("description", "")), source,
            tuple(device_ids), points, tuple(mappings), gateways,
        )
    canonical = json.dumps({"service": service, "instance": instance},
                           sort_keys=True, ensure_ascii=False).encode("utf-8")
    return Settings(root, service, scenarios, hashlib.sha256(canonical).hexdigest())
