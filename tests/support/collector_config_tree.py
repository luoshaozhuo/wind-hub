"""Build immutable Deployment-selected Collector configuration trees for system tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED = REPO_ROOT / "configs"
SYSTEM_ADMIN_TOKEN = "system-test-admin-token"

SERVICE_NAME = "service_collector-realtime_v1.1.0.yaml"
SERVICE_ID = "collector-realtime"
SERVICE_VERSION = "1.1.0"
DEVICE_INSTANCE_ID = "system"
TASK_INSTANCE_ID = "system"
INSTANCE_VERSION = "1.0.0"
BUSINESS_CATALOG_VERSION = "1.4.0"
UNITS_CATALOG_VERSION = "1.2.0"


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected mapping document")
    return data


def _empty_tree(base: Path) -> Path:
    root = base / "v2"
    for name in ("services", "instances", "templates"):
        shutil.copytree(SHIPPED / name, root / name)
    (root / "deployments").mkdir(parents=True)
    return root


def _identity(root: Path, site_id: str, devices: dict[str, Any], tasks: dict[str, Any]) -> None:
    _dump(
        root / "instances" / f"site_{site_id}_v{INSTANCE_VERSION}.yaml",
        {
            "schema_version": 1,
            "file_version": INSTANCE_VERSION,
            "site_id": site_id,
            "name": f"System {site_id}",
        },
    )
    _dump(
        root / "instances" / f"device_{DEVICE_INSTANCE_ID}_v{INSTANCE_VERSION}.yaml",
        {"schema_version": 1, "file_version": INSTANCE_VERSION, "devices": devices},
    )
    _dump(
        root / "instances" / f"collector_tasks_{TASK_INSTANCE_ID}_v{INSTANCE_VERSION}.yaml",
        {"schema_version": 1, "file_version": INSTANCE_VERSION, "tasks": tasks},
    )


def _service(root: Path, site_id: str, nats_url: str, settings: dict[str, Any]) -> Path:
    path = root / "services" / SERVICE_NAME
    service = _load(path)
    service["service_id"] = SERVICE_ID
    service["config_sources"] = {
        "instances": {
            "site": site_id,
            "devices": DEVICE_INSTANCE_ID,
            "tasks": TASK_INSTANCE_ID,
        }
    }
    service["messaging"]["servers"] = [nats_url]
    for key, value in settings.items():
        if key == "messaging":
            service["messaging"].update(value)
        elif isinstance(value, dict):
            service.setdefault(key, {}).update(value)
        else:
            service[key] = value
    _dump(path, service)
    _dump(
        root / "deployments" / "deployment_collector-system_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "deployment_id": "collector-system",
            "services": {SERVICE_ID: {"config_id": SERVICE_ID, "file_version": SERVICE_VERSION}},
            "instances": {
                "site": {site_id: {"file_version": INSTANCE_VERSION}},
                "device": {DEVICE_INSTANCE_ID: {"file_version": INSTANCE_VERSION}},
                "collector_tasks": {TASK_INSTANCE_ID: {"file_version": INSTANCE_VERSION}},
            },
        },
    )
    return root


def write_collector_modbus_tree(
    base: Path,
    port: int,
    *,
    device_id: str = "modbus-1",
    site_id: str = "test",
    desired_state: str = "STOP",
    nats_url: str = "nats://127.0.0.1:4222",
    slave_id: int = 1,
) -> Path:
    """One real Modbus device, its Instance-owned task, and exact Deployment."""
    root = _empty_tree(base)
    points = {
        "rotor_speed": ("float32", 100),
        "gen_power": ("float32", 102),
        "temp_int": ("int16", 104),
        "setpoint_power": ("float32", 200),
    }
    _dump(
        root / "templates" / f"business_points_standard_v{BUSINESS_CATALOG_VERSION}.yaml",
        {
            "schema_version": 1,
            "file_version": BUSINESS_CATALOG_VERSION,
            "business_points": {
                point: {"data_type": data_type, "unit": "none"}
                for point, (data_type, _address) in points.items()
            },
        },
    )
    _dump(
        root / "templates/point_tables/point_table_modbus_wtg_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "template_id": "modbus_wtg",
            "protocol": "modbus",
            "points": {
                point: {
                    "business_point_id": point,
                    "point_groups": ["all"],
                    "address": {"register_type": "holding", "address": address},
                    "data_type": data_type,
                    "unit": "none",
                }
                for point, (data_type, address) in points.items()
            },
        },
    )
    _dump(
        root / "templates/device_models/device_model_modbus_wtg_v1.0.0.yaml",
        {
            "schema_version": 1,
            "file_version": "1.0.0",
            "template_id": "modbus_wtg",
            "name": "System Modbus turbine",
            "device_type_id": "turbine",
            "protocol": {"id": "modbus_tcp", "version": "1.1.0"},
            "point_table": {"id": "modbus_wtg", "version": "1.0.0"},
            "protocol_options": {"slave_id": slave_id, "word_order": "big_endian"},
        },
    )
    _identity(
        root,
        site_id,
        {
            device_id: {
                "enabled": True,
                "group": "turbine",
                "model": {"id": "modbus_wtg", "version": "1.0.0"},
                "endpoint": {"host": "127.0.0.1", "port": port},
                "overrides": {"protocol_options": {"slave_id": slave_id}},
            }
        },
        {
            "turbine_all": {
                "group": "turbine",
                "point_group": "all",
                "interval_ms": 200,
                "desired_state": desired_state,
            }
        },
    )
    return _service(
        root,
        site_id,
        nats_url,
        {
            "runtime": {
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "shutdown_timeout": 5.0,
            },
            "watchdog": {"interval": 0.5, "health_timeout": 0.5},
        },
    )


def build_collector_tree_from_site(
    base: Path,
    flat_site: Path,
    *,
    nats_url: str = "nats://127.0.0.1:4222",
) -> Path:
    """Translate a legacy *input fixture* into the current published closure."""
    root = _empty_tree(base)
    points_doc = _load(flat_site / "points.yaml")
    models_doc = _load(flat_site / "device_models.yaml")
    devices_doc = _load(flat_site / "devices.yaml")
    system_doc = _load(flat_site / "system.yaml")
    tasks_path = flat_site / "tasks.yaml"
    tasks_doc = _load(tasks_path) if tasks_path.exists() else {"tasks": {}}

    business = _load(flat_site / "business_points.yaml")["business_points"]
    units = _load(flat_site / "units.yaml")
    _dump(
        root / "templates" / f"business_points_standard_v{BUSINESS_CATALOG_VERSION}.yaml",
        {
            "schema_version": 1,
            "file_version": BUSINESS_CATALOG_VERSION,
            "business_points": business,
        },
    )
    _dump(
        root / "templates" / f"units_standard_v{UNITS_CATALOG_VERSION}.yaml",
        {"schema_version": 1, "file_version": UNITS_CATALOG_VERSION, **units},
    )

    models = models_doc["device_models"]
    for protocol in sorted({model["protocol"] for model in models.values()}):
        _dump(
            root / "templates/protocols" / f"protocol_{protocol}_default_v1.0.0.yaml",
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "template_id": f"{protocol}_default",
                "protocol": protocol,
            },
        )
    for table_id, table in points_doc["point_tables"].items():
        result: dict[str, Any] = {}
        for point_id, point in table["points"].items():
            unit = point.get("unit", business.get(point_id, {}).get("unit", "none"))
            result[point_id] = {
                **point,
                "business_point_id": point.get("business_point_id", point_id),
                "point_groups": sorted(set(point.get("point_groups", [])) | {"all"}),
                "unit": unit,
            }
        _dump(
            root / "templates/point_tables" / f"point_table_{table_id}_v1.0.0.yaml",
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "template_id": table_id,
                "protocol": table["protocol"],
                "points": result,
            },
        )
    for model_id, model in models.items():
        document = {
            "schema_version": 1,
            "file_version": "1.0.0",
            "template_id": model_id,
            "name": model_id,
            "device_type_id": model.get("device_type_id", "turbine"),
            "protocol": {"id": f"{model['protocol']}_default", "version": "1.0.0"},
            "point_table": {"id": model["point_table_id"], "version": "1.0.0"},
        }
        for optional in ("protocol_options", "endpoint_defaults"):
            if model.get(optional):
                document[optional] = model[optional]
        _dump(root / "templates/device_models" / f"device_model_{model_id}_v1.0.0.yaml", document)

    # Task policy is specified by the collector_tasks Instance, never by models.
    group_specs: dict[str, dict[str, Any]] = {}
    for task in tasks_doc.get("tasks", {}).values():
        group = str(task["device_group_id"])
        interval = float(task.get("interval", 1.0))
        group_specs[group] = {
            "group": group,
            "point_group": "all",
            "interval_ms": max(1, round(interval * 1000)),
            "desired_state": str(task.get("desired_state", "STOP")),
        }
    devices: dict[str, Any] = {}
    for device_id, source in devices_doc["devices"].items():
        groups = [str(group) for group in source.get("device_group_ids", [])]
        group = next((g for g in groups if g in group_specs), groups[0] if groups else device_id)
        group_specs.setdefault(
            group,
            {"group": group, "point_group": "all", "interval_ms": 1000, "desired_state": "STOP"},
        )
        device = {
            "enabled": source.get("enabled", True),
            "group": group,
            "model": {"id": source["model_id"], "version": "1.0.0"},
            "endpoint": dict(source["endpoint"]),
        }
        if source.get("protocol_options"):
            device["overrides"] = {"protocol_options": dict(source["protocol_options"])}
        devices[device_id] = device

    site = system_doc.get("site", {})
    site_id = str(site.get("site_id", "virtual_windfarm"))
    _identity(
        root,
        site_id,
        devices,
        {f"task_{idx:02d}": spec for idx, spec in enumerate(group_specs.values())},
    )
    messaging = system_doc.get("messaging", {})
    runtime = system_doc.get("runtime", {})
    service_settings: dict[str, Any] = {
        "messaging": {
            "delivery": messaging.get("delivery", "realtime"),
            "max_staleness_ms": messaging.get("max_staleness_ms", 60_000),
        },
        "runtime": {
            "connect_timeout": runtime.get("connect_timeout", 10.0),
            "read_timeout": runtime.get("read_timeout", 5.0),
            "write_timeout": runtime.get("write_timeout", 5.0),
            "shutdown_timeout": runtime.get("shutdown_timeout", 30.0),
        },
    }
    if runtime.get("watchdog_interval") or runtime.get("health_timeout"):
        service_settings["watchdog"] = {
            "interval": runtime.get("watchdog_interval", 1.0),
            "health_timeout": runtime.get("health_timeout", 1.0),
        }
    if runtime.get("queue_maxsize"):
        service_settings["metrics"] = {"pending_capacity": int(runtime["queue_maxsize"])}
    if system_doc.get("ads"):
        service_settings["ads"] = system_doc["ads"]
    return _service(root, site_id, nats_url, service_settings)
