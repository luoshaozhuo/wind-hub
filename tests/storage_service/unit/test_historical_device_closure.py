"""Unknown historical devices are resolved by immutable full Configuration closure."""

import asyncio
import hashlib
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from storage_service.application.config import TypeMapping
from storage_service.domain.wide_record import WideDataLevel, WideIdentity
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigurationPointTableResolver,
    RepositoryDocument,
)


def _doc(kind: str, identity: str, version: str, body: dict[str, object]) -> RepositoryDocument:
    payload = {"schema_version": 1, "file_version": version, **body}
    blob = yaml.safe_dump(payload, sort_keys=True).encode("utf-8")
    return RepositoryDocument(
        config_type=kind,
        config_id=identity,
        version_id=f"{kind}:{identity}:{version}",
        revision_id=1,
        yaml_bytes=blob,
        yaml_sha256=hashlib.sha256(blob).hexdigest(),
    )


def _closure() -> tuple[RepositoryDocument, ...]:
    return (
        _doc(
            "device",
            "farm-a",
            "1.0.1",
            {"devices": {"GW01": {"model": {"id": "wtg", "version": "1.0.0"}}}},
        ),
        _doc(
            "device_model",
            "wtg",
            "1.0.0",
            {
                "template_id": "wtg",
                "device_type_id": "turbine",
                "point_table": {"id": "wind", "version": "1.0.0"},
            },
        ),
        _doc(
            "point_table",
            "wind",
            "1.0.0",
            {
                "template_id": "wind",
                "protocol": "modbus",
                "points": {
                    "wind_speed": {
                        "data_type": "float32",
                        "business_point_id": "wind_speed_std",
                        "address": {"count": 2},
                    }
                },
                "config_sources": [
                    {
                        "config_type": "business_points",
                        "config_id": "standard",
                        "file_version": "1.0.0",
                    },
                    {"config_type": "units", "config_id": "standard", "file_version": "1.0.0"},
                ],
            },
        ),
        _doc(
            "business_points",
            "standard",
            "1.0.0",
            {"business_points": {"wind_speed_std": {"data_type": "float32"}}},
        ),
        _doc("units", "standard", "1.0.0", {"units": {}}),
    )


def _mapping() -> TypeMapping:
    return TypeMapping(
        bool="BOOL",
        int8="TINYINT",
        uint8="TINYINT UNSIGNED",
        int16="SMALLINT",
        uint16="SMALLINT UNSIGNED",
        int32="INT",
        uint32="INT UNSIGNED",
        int64="BIGINT",
        uint64="BIGINT UNSIGNED",
        float32="FLOAT",
        float64="DOUBLE",
        string="VARCHAR",
        bytes="VARBINARY",
    )


@pytest.mark.asyncio
async def test_unknown_historical_device_is_persisted_before_wide_schema_use() -> None:
    resolver = object.__new__(ConfigurationPointTableResolver)
    resolver._device_bindings = {}
    resolver._exact_binding_cache = {}
    resolver._wide_schema_cache = {}
    resolver._pins = {}
    resolver._business_pins = {}
    resolver._loaded = {}
    resolver._lock = asyncio.Lock()
    resolver._refresh_hook = None
    resolver._persist_historical = Mock()
    resolver._fetcher = Mock()
    resolver._fetcher.fetch_exact_device_closure = AsyncMock(return_value=_closure())
    resolver._load = AsyncMock(
        return_value={
            "schema_version": 1,
            "template_id": "wind",
            "file_version": "1.0.0",
            "protocol": "modbus",
            "points": {"wind_speed": {"data_type": "float32", "address": {"count": 2}}},
        }
    )
    identity = WideIdentity(
        wind_farm_id="farm_a",
        device_config_id="device_farm-a_v1.0.1",
        device_id="GW01",
        data_level=WideDataLevel.PROTOCOL,
    )
    schema = await resolver.resolve_wide_schema(
        identity, type_mapping=_mapping(), max_row_bytes=65536
    )
    assert schema.point_table_id == "point_table_wind_v1.0.0"
    assert tuple(column.point_id for column in schema.columns) == ("wind_speed",)
    assert resolver.wide_device_type(identity) == "turbine"
    resolver._persist_historical.assert_called_once()
    pins, business, binding = resolver._persist_historical.call_args.args
    assert pins == {"point_table_wind_v1.0.0": ("wind", "point_table:wind:1.0.0")}
    assert business == ("standard", "business_points:standard:1.0.0")
    assert binding.site_config_id == ""
    assert binding.device_version_id == "device:farm-a:1.0.1"
