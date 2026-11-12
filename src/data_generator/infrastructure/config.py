"""Scenario validation resolved against the authoritative Core shared snapshot."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from configuration.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
    SharedDocuments,
)
from configuration.immutable import plain_document
from configuration.shared_snapshot import (
    SharedDomainSnapshot,
    build_shared_snapshot,
)
from core.domain import DeviceId
from data_generator.application.model.common import is_real_number
from data_generator.application.model.random import validate_rule
from data_generator.application.models import interpolation_table, random_value
from data_generator.domain.model import GeneratorError, OutputMapping, OutputTarget
from data_generator.domain.scenario import Scenario, Settings

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GeneratorError(f"{path}: {exc}") from exc
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int:
        raise GeneratorError(f"{path}: expected schema_version integer")
    if data["schema_version"] != 1 or not isinstance(data.get("file_version"), str):
        raise GeneratorError(f"{path}: unsupported document version")
    if not _VERSION.fullmatch(data["file_version"]):
        raise GeneratorError(f"{path}: expected MAJOR.MINOR.PATCH file_version")
    return data


def _resolve_points(
    raw: dict[str, Any], shared: SharedDomainSnapshot
) -> tuple[tuple[str, ...], dict[tuple[str, str], dict[str, Any]]]:
    devices: list[str] = []
    points: dict[tuple[str, str], dict[str, Any]] = {}
    for device in raw.get("devices", []):
        if not isinstance(device, dict):
            raise GeneratorError("invalid device mapping")
        device_id = device.get("device_id")
        if not isinstance(device_id, str) or not device_id or device_id in devices:
            raise GeneratorError("duplicate or invalid device ID")
        shared_device = shared.site.devices.get(DeviceId(device_id))
        if shared_device is None or not shared_device.enabled:
            raise GeneratorError(f"unknown or disabled shared device: {device_id}")
        model = shared.device_models[shared_device.device_model_id]
        table = shared.point_tables[model.point_table_id]
        devices.append(device_id)
        for point in device.get("points", []):
            if not isinstance(point, dict):
                raise GeneratorError("invalid scenario point")
            point_id = point.get("point_id")
            if not isinstance(point_id, str) or point_id not in table.points:
                raise GeneratorError(f"{device_id}: unknown shared point: {point_id}")
            key = (device_id, point_id)
            if key in points:
                raise GeneratorError(f"duplicate scenario point: {key}")
            if any(field in point for field in ("data_type", "unit", "protocol", "address")):
                raise GeneratorError("scenario must not redefine shared point metadata")
            defined = table.points[point_id]
            business = shared.business_points[defined.business_point_id]
            effective = dict(point)
            effective["data_type"] = business.data_type.value
            if effective.get("model") is not None and not isinstance(effective["model"], dict):
                raise GeneratorError("invalid model parameters")
            points[key] = effective
    if not points:
        raise GeneratorError("scenario has no points")
    return tuple(devices), points


def _configured_model_points(
    raw: dict[str, Any],
    shared: SharedDomainSnapshot,
    documents: SharedDocuments,
) -> tuple[
    tuple[str, ...],
    dict[tuple[str, str], dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Bind each virtual device to its own versioned simulation model."""
    definitions = raw.get("models", {})
    if not isinstance(definitions, dict):
        raise GeneratorError("invalid random model declarations")
    devices: list[str] = []
    points: dict[tuple[str, str], dict[str, Any]] = {}
    bindings: dict[str, dict[str, Any]] = {}
    models: dict[str, dict[str, Any]] = {}
    expected_types = {
        "wtg": "turbine",
        "pv": "photovoltaic",
        "bess": "energy_storage",
        "grid": "grid",
        "ppc": "plant_controller",
    }
    for item in raw.get("devices", []):
        if not isinstance(item, dict):
            raise GeneratorError("invalid scenario device binding")
        device_id = item.get("device_id")
        if not isinstance(device_id, str) or not device_id or device_id in bindings:
            raise GeneratorError("duplicate or invalid scenario device")
        shared_device = shared.site.devices.get(DeviceId(device_id))
        document_device = documents.devices.get(device_id)
        if shared_device is None or not shared_device.enabled or document_device is None:
            raise GeneratorError(f"{device_id}: unknown or disabled shared device")
        model = shared.device_models[shared_device.device_model_id]
        table = shared.point_tables[model.point_table_id]
        declared = item.get("model_ref")
        if declared is not None:
            definition = definitions.get(declared)
            if not isinstance(definition, dict) or definition.get("kind") != "random":
                raise GeneratorError(f"{device_id}: invalid random model reference")
            if "simulation" in document_device.templates.model.document:
                raise GeneratorError("random source cannot override a physical simulation model")
            model_ref = str(declared)
        else:
            simulation = document_device.templates.model.document.get("simulation")
            if not isinstance(simulation, Mapping) or simulation.get("kind") != "physical":
                raise GeneratorError(f"{device_id}: simulation model missing from device template")
            expected = expected_types.get(str(simulation.get("device_kind")))
            if expected is None or str(model.device_type_id) != expected:
                raise GeneratorError(f"{device_id}: device type and physical model disagree")
            parameters = simulation.get("parameters")
            if not isinstance(parameters, Mapping) or not parameters:
                raise GeneratorError(f"{device_id}: physical parameters missing")
            model_ref = device_id
            definition = {
                "kind": "physical",
                "device_kind": simulation["device_kind"],
                "parameters": plain_document(parameters),
                "weather_ref": item.get("weather_ref"),
                "controller_mode": item.get("controller_mode", "local"),
            }
        models[model_ref] = definition
        selected = item.get("points", list(table.points))
        if not isinstance(selected, list) or not selected:
            raise GeneratorError(f"{device_id}: empty selected point set")
        for point_id in selected:
            if not isinstance(point_id, str) or point_id not in table.points:
                raise GeneratorError(f"{device_id}: unknown shared point {point_id}")
            key = (device_id, point_id)
            if key in points:
                raise GeneratorError(f"duplicate scenario output point: {key}")
            point = table.points[point_id]
            business = shared.business_points[point.business_point_id]
            metadata: dict[str, Any] = {"data_type": business.data_type.value}
            if definition["kind"] == "random":
                rules = definition.get("rules")
                if not isinstance(rules, dict) or point_id not in rules:
                    raise GeneratorError(f"{device_id}: missing random rule for {point_id}")
                rule = rules[point_id]
                if not isinstance(rule, dict):
                    raise GeneratorError(f"{device_id}: invalid random rule")
                validate_rule(rule, str(metadata["data_type"]))
                seed = definition.get("seed", 0)
                if type(seed) is not int:
                    raise GeneratorError("random model seed must be integer")
                metadata["model"] = {"kind": "random", "seed": seed, **rule}
            points[key] = metadata
        devices.append(device_id)
        bindings[device_id] = {"device_id": device_id, "model_ref": model_ref}
    if not points:
        raise GeneratorError("scenario has no points")
    return tuple(devices), points, bindings, models


def _validate_models(source: dict[str, Any], points: dict[tuple[str, str], dict[str, Any]]) -> None:
    kind = source.get("kind")
    if kind == "simulation":
        interval = source.get("interval_seconds")
        if (
            not isinstance(interval, int | float)
            or isinstance(interval, bool)
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise GeneratorError("invalid simulation interval")
        if type(source.get("seed", 0)) is not int:
            raise GeneratorError("simulation seed must be integer")
        dependencies: dict[tuple[str, str], tuple[str, str] | None] = {}
        for key, point in points.items():
            model = point.get("model")
            if not isinstance(model, dict) or model.get("kind") not in ("random", "static"):
                raise GeneratorError(f"{key}: invalid simulation model")
            if model["kind"] == "random":
                random_value(0, key[0], key[1], 1, {**model, "data_type": point["data_type"]})
                dependencies[key] = None
            else:
                interpolation_table(model)
                dependency = (key[0], model.get("input_point"))
                if dependency not in points:
                    raise GeneratorError("missing static input point")
                dependencies[key] = dependency
        for key in dependencies:
            visited: set[tuple[str, str]] = set()
            current: tuple[str, str] | None = key
            while current is not None:
                if current in visited:
                    raise GeneratorError("cyclic static dependency")
                visited.add(current)
                current = dependencies[current]
    elif kind == "replay":
        if source.get("history_reader") != "csv" or source.get("loop", False) is not False:
            raise GeneratorError("only non-looping CSV replay supported")
        speed = source.get("speed")
        if (
            not isinstance(speed, int | float)
            or isinstance(speed, bool)
            or not math.isfinite(speed)
            or speed <= 0
        ):
            raise GeneratorError("invalid replay speed")
        if not isinstance(source.get("file"), str) or not source["file"]:
            raise GeneratorError("replay requires CSV path")
        try:
            first = datetime.fromisoformat(source["start_time"].replace("Z", "+00:00"))
            last = datetime.fromisoformat(source["end_time"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError) as exc:
            raise GeneratorError("invalid replay time range") from exc
        if (
            first.tzinfo is None
            or last.tzinfo is None
            or first >= last
            or first.utcoffset() != last.utcoffset()
            or first.utcoffset() != timedelta(0)
        ):
            raise GeneratorError("replay window must be valid UTC")
    else:
        raise GeneratorError("unsupported source kind")


def validate_service_limits(service: dict[str, Any]) -> None:
    """Validate resource budgets before admitting any generation or configuration candidate."""
    limits: dict[str, dict[str, tuple[float, float]]] = {
        "output": {
            "max_inflight": (1, 128),
            "grpc_timeout_seconds": (0.01, 60.0),
        },
        "buffer": {
            "max_batches": (1, 4096),
            "max_points_per_batch": (1, 5000),
        },
        "replay": {
            "read_batch_size": (1, 100000),
            "max_file_bytes": (1, 1073741824),
            "max_records": (1, 10000000),
        },
        "diagnose": {
            "max_concurrent": (1, 64),
            "timeout_seconds": (0.1, 60.0),
            "permit_wait_seconds": (0.01, 10.0),
        },
        "metrics": {
            "sample_seconds": (0.01, 60.0),
            "window_seconds": (1, 3600),
            "retain_seconds": (1, 86400),
            "max_series": (1, 10000),
        },
    }
    for section, settings in limits.items():
        configured = service.get(section, {})
        if not isinstance(configured, dict):
            raise GeneratorError(f"service {section} must be a mapping")
        for key, (lower, upper) in settings.items():
            if key not in configured:
                continue
            value = configured[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or not lower <= value <= upper
                or (isinstance(lower, int) and type(value) is not int)
            ):
                raise GeneratorError(f"invalid {section}.{key}: must be within {lower}..{upper}")
    recording = service.get("recording", {})
    if not isinstance(recording, dict):
        raise GeneratorError("recording must be a mapping")
    if type(recording.get("enabled", False)) is not bool:
        raise GeneratorError("recording.enabled must be a boolean")
    directory = recording.get("directory", "artifacts/data-generator")
    if (
        not isinstance(directory, str)
        or not directory
        or Path(directory).is_absolute()
        or ".." in Path(directory).parts
        or "\\\\" in directory
    ):
        raise GeneratorError("recording.directory must be project-relative")
    recording["directory"] = directory
    metrics = service.get("metrics", {})
    if metrics.get("window_seconds", 60) < metrics.get("sample_seconds", 1.0):
        raise GeneratorError("metrics window must cover sampling interval")
    if metrics.get("retain_seconds", 3600) < metrics.get("window_seconds", 60):
        raise GeneratorError("metrics retention must cover window")


def resolve_source(service_file: Path, value: object, *, directory: bool = False) -> Path:
    """Resolve an explicitly declared path relative to configs/, never services/."""
    if not isinstance(value, str) or not value.strip():
        raise GeneratorError("configuration source path must be a nonempty string")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise GeneratorError("configuration path must be configs-relative without traversal")
    root = service_file.parent.parent.resolve()
    if relative.parts and relative.parts[0] == "configs":
        target = root.joinpath(*relative.parts[1:]).resolve()
    else:
        target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise GeneratorError("configuration path escapes configs root")
    if directory:
        if not target.is_dir():
            raise GeneratorError(f"template directory not found: {target}")
    elif not target.is_file():
        raise GeneratorError(f"configuration document not found: {target}")
    return target


def config_sources(service_file: Path, service: dict[str, Any]) -> dict[str, Path]:
    configuration = service.get("config_sources")
    if not isinstance(configuration, dict) or set(configuration) != {
        "instances",
        "templates",
    }:
        raise GeneratorError("service.config_sources requires instances and templates")
    instances = configuration["instances"]
    templates = configuration["templates"]
    if not isinstance(instances, dict) or set(instances) != {"site", "devices"}:
        raise GeneratorError("instances requires site and devices paths")
    if not isinstance(templates, dict) or set(templates) != {"root"}:
        raise GeneratorError("templates requires only root")
    resolved = {
        key: resolve_source(service_file, instances[key])
        for key in ("site", "devices")
    }
    resolved["templates_root"] = resolve_source(
        service_file,
        templates["root"],
        directory=True,
    )
    resolved["units"] = resolved["templates_root"] / "units.yaml"
    resolved["business_points"] = resolved["templates_root"] / "business_points.yaml"
    # Catalog files are Core's indirect dependencies, not service declarations.
    for name in ("units", "business_points"):
        if not resolved[name].is_file():
            raise GeneratorError(f"required Core catalog absent: {resolved[name]}")
    return resolved


def load_settings(service_path: Path) -> Settings:
    path = service_path.resolve(strict=True)
    if path.parent.name != "services":
        raise GeneratorError("service configuration must be inside services/")
    root = path.parent.parent
    service = load_yaml(path)
    validate_service_limits(service)
    paths = config_sources(path, service)
    try:
        documents = SharedDocumentLoader(
            SharedConfigPaths(
                instances=paths["site"].parent,
                templates=paths["templates_root"],
                site_file=paths["site"],
                devices_file=paths["devices"],
            )
        ).load()
        shared = build_shared_snapshot(documents)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise GeneratorError(f"invalid shared configuration: {exc}") from exc
    raw_scenarios = service.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise GeneratorError("at least one scenario required")
    scenarios: dict[str, Scenario] = {}
    for raw in raw_scenarios:
        if not isinstance(raw, dict):
            raise GeneratorError("invalid scenario")
        scenario_id, version = raw.get("scenario_id"), raw.get("version")
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in scenarios:
            raise GeneratorError("duplicate or invalid scenario ID")
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise GeneratorError("invalid scenario version")
        source = raw.get("source")
        if not isinstance(source, dict):
            raise GeneratorError("invalid scenario source")
        if source.get("kind") == "simulation" and (
            "models" in raw
            or any(
                isinstance(item, dict) and (
                    "model_ref" in item or "points" not in item
                )
                for item in raw.get("devices", [])
            )
        ):
            if source.get("kind") != "simulation":
                raise GeneratorError("configured models require simulation source")
            devices, points, bindings, models = _configured_model_points(raw, shared, documents)
            interval = source.get("interval_seconds")
            if not is_real_number(interval) or not math.isfinite(interval) or interval <= 0:
                raise GeneratorError("simulation interval must be positive")
            if type(source.get("seed", 0)) is not int:
                raise GeneratorError("simulation seed must be integer")
            weather = raw.get("weather", {})
            if not isinstance(weather, dict):
                raise GeneratorError("invalid weather mapping")
        else:
            devices, points = _resolve_points(raw, shared)
            _validate_models(source, points)
            bindings, models, weather = {}, {}, {}
        gateways: dict[str, str] = {}
        mappings: list[OutputMapping] = []
        targets: set[OutputTarget] = set()
        for gateway in raw.get("gateways", []):
            if not isinstance(gateway, dict):
                raise GeneratorError("invalid Gateway route")
            gateway_id = gateway.get("gateway_id")
            endpoint = gateway.get("endpoint")
            device_id = gateway.get("device_id")
            if not isinstance(gateway_id, str) or not gateway_id or gateway_id in gateways:
                raise GeneratorError("duplicate Gateway ID")
            if not isinstance(endpoint, str) or not endpoint:
                raise GeneratorError("invalid Gateway endpoint")
            if device_id not in devices:
                raise GeneratorError("Gateway route references unknown scenario device")
            if "feedback_mapping" in gateway:
                raise GeneratorError("Gateway point mapping belongs to Gateway configuration")
            gateways[gateway_id] = endpoint
            for source_device, point_id in points:
                if source_device != device_id:
                    continue
                target = OutputTarget(gateway_id, point_id)
                if target in targets:
                    raise GeneratorError(f"duplicate output target: {target}")
                targets.add(target)
                mappings.append(OutputMapping(source_device, point_id, target))
        if not mappings:
            raise GeneratorError("scenario requires Gateway device routes")
        if set(points) - {(item.device_id, item.point_id) for item in mappings}:
            raise GeneratorError("enabled source point lacks Gateway device route")
        scenarios[scenario_id] = Scenario(
            scenario_id,
            version,
            str(raw.get("description", "")),
            source,
            devices,
            points,
            tuple(mappings),
            gateways,
            models,
            weather,
            bindings,
        )
        if any(definition.get("kind") == "physical" for definition in models.values()):
            from data_generator.application.physical_engine import PhysicalEngine

            PhysicalEngine(scenarios[scenario_id])
    payload = json.dumps(
        {
            "service_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "shared_revision": shared.revision,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return Settings(
        root,
        service,
        scenarios,
        hashlib.sha256(payload).hexdigest(),
        shared,
        path,
        paths["site"],
        paths["devices"],
        paths["units"],
        paths["business_points"],
        paths["templates_root"],
    )
