"""Build complete, schema-valid Collector configuration trees for real processes.

Every YAML document carries ``file_version`` and the Service uses messaging
schema 6. This fixture uses isolated versions.
The §11 Template/Service/Instance tree is consumed by a real
``collector.main`` process, never by in-process fakes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_HEADER = {"schema_version": 1, "file_version": "1.0.0"}


def _dump(document: dict[str, Any]) -> bytes:
    return yaml.safe_dump(document, sort_keys=False).encode("utf-8")


def write_config_tree(
    root: Path,
    *,
    modbus_port: int,
    iec104_port: int,
    nats_servers: list[str],
    delivery: str = "realtime",
    desired_state: str = "STOP",
    devices: tuple[str, ...] = ("modbus-1", "iec104-1"),
    disabled_devices: tuple[str, ...] = (),
    watchdog_interval: float = 0.3,
    max_staleness_ms: int = 5000,
    task_interval: float = 0.2,
    blackhole_port: int | None = None,
    hold_desired_state: str = "STOP",
    shutdown_timeout: float = 10,
    site_ref: str = "site_alpha",
    devices_ref: str = "farm-a",
    templates_ref: str = "templates",
) -> Path:
    """Write services/instances/templates roots; return the services directory."""
    services = root / "services"
    instances = root / "instances"
    templates = root / "templates"
    for directory in (services, instances, templates):
        directory.mkdir(parents=True, exist_ok=True)

    if templates_ref != "templates":
        raise ValueError("Template roots are no longer selected by Service")
    service_id = f"collector-{delivery}"
    service = {
        **_HEADER,
        "schema_version": 6,
        "service_id": service_id,
        "config_sources": {
            "instances": {"site": site_ref, "devices": devices_ref, "tasks": "farm-a"},
        },
        "runtime": {
            "shutdown_timeout": shutdown_timeout,
            "connect_timeout": 3,
            "read_timeout": 2,
            "thread_pool": {"max_workers": 8},
        },
        "watchdog": {"interval": watchdog_interval, "health_timeout": 1},
        "messaging": {
            "servers": nats_servers,
            "delivery": delivery,
            **({"max_staleness_ms": max_staleness_ms} if delivery == "realtime" else {}),
        },
        "metrics": {
            "enabled": True,
            "windows": [60, 300, 3600],
            "publish_interval": 1,
            "max_staleness": 300,
            "event_capacity": 1000,
            "pending_capacity": 256,
        },
    }
    (services / f"service_{service_id}_v1.0.0.yaml").write_bytes(_dump(service))

    site = {**_HEADER, "site_id": site_ref, "name": "Acceptance Site"}
    site_path = instances / f"site_{site_ref}_v1.0.0.yaml"
    site_path.parent.mkdir(parents=True, exist_ok=True)
    site_path.write_bytes(_dump(site))

    device_doc: dict[str, Any] = {**_HEADER, "devices": {}}
    if "modbus-1" in devices:
        device_doc["devices"]["modbus-1"] = {
            "model": {"id": "modbus_wtg", "version": "1.0.0"},
            "endpoint": {"host": "127.0.0.1", "port": modbus_port},
            "enabled": "modbus-1" not in disabled_devices,
            "group": "modbus-group",
            "overrides": {"task": {"desired_state": desired_state}},
        }
    if "iec104-1" in devices:
        device_doc["devices"]["iec104-1"] = {
            "model": {"id": "iec104_wtg", "version": "1.0.0"},
            "endpoint": {"host": "127.0.0.1", "port": iec104_port},
            "enabled": "iec104-1" not in disabled_devices,
            "group": "iec104-group",
            "overrides": {"task": {"desired_state": desired_state}},
        }
    if blackhole_port is not None:
        device_doc["devices"]["modbus-hold"] = {
            "model": {"id": "modbus_wtg", "version": "1.0.0"},
            "endpoint": {"host": "127.0.0.1", "port": blackhole_port},
            "group": "hold",
        }
    devices_path = instances / f"device_{devices_ref}_v1.0.0.yaml"
    devices_path.parent.mkdir(parents=True, exist_ok=True)
    devices_path.write_bytes(_dump(device_doc))

    (templates / "units_standard_v1.2.0.yaml").write_bytes(
        _dump(
            {
                **_HEADER,
                "file_version": "1.2.0",
                "quantities": {
                    "dimensionless": {"base_unit": "none"},
                    "active_power": {"base_unit": "watt"},
                    "rotor_speed": {"base_unit": "rpm"},
                    "temperature": {"base_unit": "celsius"},
                    "frequency": {"base_unit": "hertz"},
                },
                "units": {
                    "none": {"symbol": "", "quantity": "dimensionless"},
                    "watt": {"symbol": "W", "quantity": "active_power"},
                    "kilowatt": {
                        "symbol": "kW",
                        "quantity": "active_power",
                        "scale_to_base": 1000,
                    },
                    "rpm": {"symbol": "rpm", "quantity": "rotor_speed"},
                    "celsius": {"symbol": "°C", "quantity": "temperature"},
                    "hertz": {"symbol": "Hz", "quantity": "frequency"},
                },
            }
        )
    )
    (templates / "business_points_standard_v1.4.0.yaml").write_bytes(
        _dump(
            {
                **_HEADER,
                "file_version": "1.4.0",
                "business_points": {
                    "active_power": {"data_type": "int32", "unit": "kilowatt"},
                    "rotor_speed": {"data_type": "float32", "unit": "rpm"},
                    "gear_temp": {"data_type": "int16", "unit": "celsius"},
                    "meas_power": {"data_type": "float32", "unit": "none"},
                    "meas_freq": {"data_type": "float32", "unit": "none"},
                },
            }
        )
    )

    def template(category: str, template_id: str, document: dict[str, Any]) -> None:
        folder = templates / category
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"template_id": template_id, **document}
        (folder / f"{category[:-1]}_{template_id}_v1.0.0.yaml").write_bytes(_dump(payload))

    template("protocols", "modbus_tcp", {**_HEADER, "protocol": "modbus"})
    template("protocols", "iec104_tcp", {**_HEADER, "protocol": "iec104"})
    template(
        "device_models",
        "modbus_wtg",
        {
            **_HEADER,
            "name": "Acceptance Modbus Turbine",
            "device_type_id": "turbine",
            "protocol": {"id": "modbus_tcp", "version": "1.0.0"},
            "point_table": {"id": "modbus_wtg", "version": "1.0.0"},
            "protocol_options": {"slave_id": 1, "word_order": "little_endian"},
        },
    )
    template(
        "device_models",
        "iec104_wtg",
        {
            **_HEADER,
            "name": "Acceptance IEC104 Turbine",
            "device_type_id": "turbine",
            "protocol": {"id": "iec104_tcp", "version": "1.0.0"},
            "point_table": {"id": "iec104_wtg", "version": "1.0.0"},
            "protocol_options": {"t0": 3.0, "t1": 3.0, "t2": 1.0, "t3": 10.0},
        },
    )
    template(
        "point_tables",
        "modbus_wtg",
        {
            **_HEADER,
            "protocol": "modbus",
            "points": {
                "active_power": {
                    "business_point_id": "active_power",
                    "point_groups": ["all"],
                    "address": {"register_type": "holding", "address": 0},
                    "data_type": "int32",
                    "unit": "kilowatt",
                },
                "rotor_speed": {
                    "business_point_id": "rotor_speed",
                    "point_groups": ["all"],
                    "address": {"register_type": "holding", "address": 2},
                    "data_type": "float32",
                    "unit": "rpm",
                },
                "gear_temp": {
                    "business_point_id": "gear_temp",
                    "point_groups": ["all"],
                    "address": {"register_type": "holding", "address": 4},
                    "data_type": "int16",
                    "unit": "celsius",
                },
            },
        },
    )
    template(
        "point_tables",
        "iec104_wtg",
        {
            **_HEADER,
            "protocol": "iec104",
            "points": {
                "meas_power": {
                    "business_point_id": "meas_power",
                    "point_groups": ["all"],
                    "address": {"ca": 1, "type": "M_ME_NC_1", "ioa": 100},
                    "data_type": "float32",
                    "unit": "none",
                },
                "meas_freq": {
                    "business_point_id": "meas_freq",
                    "point_groups": ["all"],
                    "address": {"ca": 1, "type": "M_ME_NC_1", "ioa": 200},
                    "data_type": "float32",
                    "unit": "none",
                },
            },
        },
    )
    (instances / "collector_tasks_farm-a_v1.0.0.yaml").write_bytes(
        _dump(
            {
                **_HEADER,
                "tasks": {
                    **(
                        {
                            "modbus_task": {
                                "group": "modbus-group",
                                "point_group": "all",
                                "interval_ms": max(1, round(task_interval * 1000)),
                                "desired_state": desired_state,
                            }
                        }
                        if "modbus-1" in devices
                        else {}
                    ),
                    **(
                        {
                            "iec104_task": {
                                "group": "iec104-group",
                                "point_group": "all",
                                "interval_ms": max(1, round(task_interval * 1000)),
                                "desired_state": desired_state,
                            }
                        }
                        if "iec104-1" in devices
                        else {}
                    ),
                    **(
                        {
                            "hold_task": {
                                "group": "hold",
                                "point_group": "all",
                                "interval_ms": max(1, round(task_interval * 1000)),
                                "desired_state": hold_desired_state,
                            }
                        }
                        if blackhole_port is not None
                        else {}
                    ),
                },
            }
        )
    )
    deployments = root / "deployments"
    deployments.mkdir(exist_ok=True)
    (deployments / "deployment_collector-acceptance_v1.0.0.yaml").write_bytes(
        _dump(
            {
                **_HEADER,
                "deployment_id": "collector-acceptance",
                "services": {service_id: {"config_id": service_id, "file_version": "1.0.0"}},
                "instances": {
                    "site": {site_ref: {"file_version": "1.0.0"}},
                    "device": {devices_ref: {"file_version": "1.0.0"}},
                    "collector_tasks": {"farm-a": {"file_version": "1.0.0"}},
                },
            }
        )
    )
    return services
