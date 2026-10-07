from __future__ import annotations

from pathlib import Path

import pytest

from core.application import (
    ConfigError,
    ConfigRevisionConflict,
    ConnectionEndpoint,
    CoreConfigSnapshot,
    DeviceConnection,
    compute_core_config_diff,
    validate_core_config,
)
from core.application.config import (
    PointAccess,
    PointTable,
    Protocol,
    ProtocolPoint,
    RawDataType,
)
from core.domain import (
    BusinessPoint,
    Device,
    DeviceModel,
    DeviceType,
    UNIT_CATALOG,
    Quantity,
    Unit,
    UnitCode,
    ValueType,
)
from core.infrastructure import (
    YamlCoreConfigCodec,
    YamlFileCoreConfigRepository,
    fingerprint_core_config,
)


def _snapshot(
    *,
    device_name: str = "WT01",
    connection_enabled: bool = True,
) -> CoreConfigSnapshot:
    device_type = DeviceType("wind_turbine", "Wind Turbine")
    point = BusinessPoint(
        "active_power",
        ValueType.FLOAT,
        UNIT_CATALOG[UnitCode.KILOWATT],
    )
    protocol_point = ProtocolPoint(
        point_id="p",
        business_point_id=point.business_point_id,
        raw_type=RawDataType("float32"),
        source_unit=UNIT_CATALOG[UnitCode.WATT],
        access=PointAccess.READ_WRITE,
    )
    table = PointTable(
        "wt_modbus",
        Protocol("modbus"),
        {protocol_point.point_id: protocol_point},
    )
    model = DeviceModel(
        "m1",
        device_type.device_type_id,
    )
    device = Device("wt01", model.device_model_id, name=device_name)
    connection = DeviceConnection(
        "wt01-main",
        device.device_id,
        ConnectionEndpoint("10.0.0.1", 502, {"unit_id": 1}),
        enabled=connection_enabled,
    )
    return CoreConfigSnapshot(
        device_types={device_type.device_type_id: device_type},
        device_models={model.device_model_id: model},
        devices={device.device_id: device},
        business_points={point.business_point_id: point},
        point_tables={table.point_table_id: table},
        device_model_point_tables={
            model.device_model_id: table.point_table_id,
        },
        device_connections={connection.connection_id: connection},
    )


def test_yaml_codec_round_trip_is_stable() -> None:
    codec = YamlCoreConfigCodec()
    snapshot = _snapshot()

    artifact = codec.encode(snapshot)
    decoded = codec.decode(artifact)

    assert decoded == snapshot
    assert codec.encode(decoded).content == artifact.content
    assert fingerprint_core_config(decoded) == fingerprint_core_config(snapshot)


def test_yaml_codec_rejects_unknown_fields() -> None:
    codec = YamlCoreConfigCodec()
    artifact = codec.encode(_snapshot())
    content = artifact.content + b"unexpected: true\n"

    with pytest.raises(ConfigError, match="unknown fields"):
        codec.decode(type(artifact)(content=content, media_type=artifact.media_type))


@pytest.mark.asyncio
async def test_yaml_file_repository_uses_revision_cas(tmp_path: Path) -> None:
    codec = YamlCoreConfigCodec()
    path = tmp_path / "core.yaml"
    path.write_bytes(codec.encode(_snapshot()).content)
    repository = YamlFileCoreConfigRepository(path, codec)

    current = await repository.load()
    updated = _snapshot(device_name="WT-01")
    stored = await repository.save(
        updated,
        expected_revision=current.revision,
    )

    assert stored.snapshot.devices["wt01"].name == "WT-01"
    assert stored.revision != current.revision

    with pytest.raises(ConfigRevisionConflict):
        await repository.save(
            _snapshot(device_name="WT-02"),
            expected_revision=current.revision,
        )



def test_snapshot_resolves_enabled_connections_only() -> None:
    enabled = _snapshot(connection_enabled=True)
    disabled = _snapshot(connection_enabled=False)

    assert len(enabled.connections_for_device("wt01")) == 1
    assert disabled.connections_for_device("wt01") == ()
    assert len(disabled.connections_for_device("wt01", enabled_only=False)) == 1



def test_core_config_rejects_noncanonical_unit_instance() -> None:
    snapshot = _snapshot()
    point = next(iter(snapshot.business_points.values()))
    invalid_point = BusinessPoint(
        point.business_point_id,
        point.value_type,
        Unit(
            UnitCode.KILOWATT,
            "bad",
            Quantity.TIME,
            scale_to_base=2.0,
        ),
    )
    invalid = CoreConfigSnapshot(
        device_types=snapshot.device_types,
        device_models=snapshot.device_models,
        devices=snapshot.devices,
        business_points={
            invalid_point.business_point_id: invalid_point,
        },
        point_tables=snapshot.point_tables,
        device_model_point_tables=snapshot.device_model_point_tables,
        device_connections=snapshot.device_connections,
    )

    with pytest.raises(ConfigError, match="canonical built-in unit"):
        validate_core_config(invalid)



def test_config_diff_detects_device_model_point_table_change() -> None:
    current = _snapshot()
    table = next(iter(current.point_tables.values()))
    replacement = PointTable(
        "wt_modbus_v2",
        table.protocol,
        table.points,
    )
    changed = CoreConfigSnapshot(
        device_types=current.device_types,
        device_models=current.device_models,
        devices=current.devices,
        business_points=current.business_points,
        point_tables={
            **current.point_tables,
            replacement.point_table_id: replacement,
        },
        device_model_point_tables={
            model_id: replacement.point_table_id
            for model_id in current.device_models
        },
        device_connections=current.device_connections,
    )

    diff = compute_core_config_diff(current, changed)

    assert diff.changed is True
    assert diff.device_model_point_tables.updated == ("m1",)



def test_core_config_requires_point_table_mapping_for_every_model() -> None:
    snapshot = _snapshot()
    invalid = CoreConfigSnapshot(
        device_types=snapshot.device_types,
        device_models=snapshot.device_models,
        devices=snapshot.devices,
        business_points=snapshot.business_points,
        point_tables=snapshot.point_tables,
        device_model_point_tables={},
        device_connections=snapshot.device_connections,
    )

    with pytest.raises(ConfigError, match="missing point table mapping"):
        validate_core_config(invalid)
