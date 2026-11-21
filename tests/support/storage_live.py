"""Real Storage process configuration helpers for CLI system tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def storage_service_document(
    *,
    farm: str,
    kafka_bootstrap: str,
    kafka_topic: str,
    kafka_group: str,
    tdengine_endpoint: str,
    grpc_port: int,
    export_root: Path,
    backup_root: Path,
    point_table_id: str = "modbus_wtg",
    point_table_version: str = "1.0.0",
    device_model_id: str = "modbus_wtg",
    device_model_version: str = "1.0.0",
    device_config_id: str = "devices",
    device_config_version: str = "1.0.0",
    site_config_id: str = "site",
    site_file_version: str = "1.0.0",
    business_points_version: str = "1.3.1",
    units_version: str = "1.1.0",
    write_timeout_ms: int = 10000,
) -> dict[str, Any]:
    """Build the central business document; secrets remain environment references."""
    document = yaml.safe_load(
        (ROOT / "configs/services/service_storage-main_v1.0.1.yaml").read_text(encoding="utf-8")
    )
    document["allowed_wind_farms"] = [farm]
    document["config_sources"] = [
        {
            "config_type": "point_table",
            "config_id": point_table_id,
            "file_version": point_table_version,
        },
        {
            "config_type": "device_model",
            "config_id": device_model_id,
            "file_version": device_model_version,
        },
        {
            "config_type": "device",
            "config_id": device_config_id,
            "file_version": device_config_version,
            "site_config_id": site_config_id,
            "site_id": farm,
            "site_file_version": site_file_version,
        },
        {
            "config_type": "site",
            "config_id": site_config_id,
            "file_version": site_file_version,
        },
        {
            "config_type": "business_points",
            "config_id": "standard",
            "file_version": business_points_version,
        },
        {
            "config_type": "units",
            "config_id": "standard",
            "file_version": units_version,
        },
    ]
    document["grpc"]["listen_port"] = grpc_port
    document["auth"]["callers"][0]["wind_farms"] = [farm]
    document["kafka"].update(
        {
            "bootstrap_servers": kafka_bootstrap,
            "topic": kafka_topic,
            "group_id": kafka_group,
        }
    )
    document["tdengine"]["endpoint"] = tdengine_endpoint
    document["tdengine"]["write_timeout_ms"] = write_timeout_ms
    document["export"]["root"] = str(export_root)
    document["backup"]["root"] = str(backup_root)
    return document


def storage_bootstrap_document(
    *,
    configuration_endpoint: str,
    nats_url: str,
    cache_directory: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "file_version": "1.0.0",
        "service_id": "storage-main",
        "configuration": {
            "endpoint": configuration_endpoint,
            "token_env": "WIND_HUB_TEST_VIEWER",
        },
        "nats": {"servers": [nats_url]},
        "cache": {"directory": str(cache_directory)},
    }
