"""Device-wide physical schema resolves from exact immutable config, not Kafka types."""

from unittest.mock import AsyncMock, Mock

import pytest

from storage_service.application.config import DeviceBinding, DeviceModel, TypeMapping
from storage_service.domain.wide_record import WideDataLevel, WideRecord
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ConfigHistoryUnavailableError,
    ConfigurationPointTableResolver,
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


def _resolver() -> ConfigurationPointTableResolver:
    fetcher = AsyncMock()
    fetcher.fetch_exact_device_closure.side_effect = ConfigHistoryUnavailableError(
        "historical binding unavailable"
    )
    resolver = ConfigurationPointTableResolver(
        fetcher=fetcher,
        archive=Mock(),
        point_table_versions={"point_table_wind_v1.2.0": ("wind", "pt-repo-identity")},
    )
    model = DeviceModel(
        config_id="wtg_model",
        version_id="model-repo-identity",
        file_version="1.0.0",
        device_type_id="wind",
        point_table_id="point_table_wind_v1.2.0",
    )
    binding = DeviceBinding(
        wind_farm_id="farm_a",
        device_id="wtg_001",
        device_config_id="farm-a",
        device_version_id="device-repo-identity",
        device_file_version="1.0.1",
        site_config_id="site-a",
        site_file_version="1.0.0",
        model=model,
    )
    resolver._device_bindings = {("farm_a", "wtg_001", "point_table_wind_v1.2.0"): (binding,)}
    resolver._pins = {"point_table_wind_v1.2.0": ("wind", "pt-repo-identity")}
    resolver._business_pins = {"point_table_wind_v1.2.0": ("standard", "business-repo-id")}
    resolver._refresh_hook = None
    resolver._load = AsyncMock(
        side_effect=[
            {
                "schema_version": 1,
                "template_id": "wind",
                "file_version": "1.2.0",
                "device_type": "wind",
                "protocol": "modbus",
                "points": {
                    "wind_speed": {
                        "data_type": "float32",
                        "business_point_id": "hub_wind_speed",
                        "address": {"register_type": "input", "count": 2},
                    },
                    "active_power": {
                        "data_type": "int32",
                        "business_point_id": "grid_active_power",
                        "address": {"register_type": "input", "count": 2},
                    },
                },
            },
            {
                "business_points": {
                    "hub_wind_speed": {"data_type": "float32", "unit": "m_per_s"},
                    "grid_active_power": {"data_type": "int32", "unit": "kw"},
                }
            },
        ],
    )
    return resolver


def _record(version: str = "device_farm-a_v1.0.1") -> WideRecord:
    return WideRecord(
        wind_farm_id="farm_a",
        device_config_id=version,
        device_id="wtg_001",
        data_level=WideDataLevel.BUSINESS,
        sample_time_us=1,
        point_ids=("grid_active_power", "hub_wind_speed"),
        values=(100, 8.2),
        quality_bitmap=b"\x05",
    )


@pytest.mark.asyncio
async def test_business_wide_schema_has_one_column_per_standard_point() -> None:
    schema = await _resolver().resolve_wide_schema(
        _record(), type_mapping=_mapping(), max_row_bytes=65536
    )
    assert tuple(column.point_id for column in schema.columns) == (
        "grid_active_power",
        "hub_wind_speed",
    )
    assert tuple(column.sql_type for column in schema.columns) == ("INT", "FLOAT")
    assert schema.point_table_version == "1.2.0"


@pytest.mark.asyncio
async def test_unknown_exact_device_version_is_not_upgraded_to_latest() -> None:
    resolver = _resolver()
    with pytest.raises(ConfigHistoryUnavailableError, match="historical binding"):
        await resolver.resolve_wide_schema(
            _record("device_farm-a_v9.0.0"),
            type_mapping=_mapping(),
            max_row_bytes=65536,
        )
    resolver._fetcher.fetch_exact_device_closure.assert_awaited_once_with(
        device_config_id="device_farm-a_v9.0.0"
    )
