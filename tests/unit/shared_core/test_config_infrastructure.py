from __future__ import annotations

from pathlib import Path

import pytest

from core.application import (
    ConfigError,
    ConfigRevisionConflict,
    ConnectionEndpoint,
    CoreConfigSnapshot,
    compute_core_config_diff,
    validate_core_config,
)
from core.domain import (
    PointAccess,
    PointTable,
    Protocol,
    Point,
    BusinessPoint,
    Device,
    DeviceModel,
    DeviceType,
    UNIT_CATALOG,
    Quantity,
    Unit,
    UnitCode,
    DataType,
)
from core.infrastructure import (
    YamlCoreConfigCodec,
    YamlFileCoreConfigRepository,
    fingerprint_core_config,
)


def _snapshot(
    *,
    device_name: str = "WT01",
) -> CoreConfigSnapshot:
    device_type = DeviceType("wind_turbine", "Wind Turbine")
    point = BusinessPoint(
        "active_power",
        DataType.FLOAT32,
        UNIT_CATALOG[UnitCode.KILOWATT],
    )
    protocol_point = Point(
        point_id="p",
        business_point_id=point.business_point_id,
        source_unit=UNIT_CATALOG[UnitCode.WATT],
        access=PointAccess.READ_WRITE,
        ext={
            "register_type": "holding",
            "address": 1,
            "data_type": "float32",
        },
    )
    table = PointTable(
        "wt_modbus",
        Protocol("modbus"),
        {protocol_point.point_id: protocol_point},
    )
    model = DeviceModel(
        "m1",
        device_type.device_type_id,
        table.point_table_id,
    )
    device = Device(
        "wt01",
        model.device_model_id,
        ConnectionEndpoint("10.0.0.1", 502),
        name=device_name,
    )
    return CoreConfigSnapshot(
        device_types={device_type.device_type_id: device_type},
        device_models={model.device_model_id: model},
        devices={device.device_id: device},
        business_points={point.business_point_id: point},
        point_tables={table.point_table_id: table},
        device_options={
            device.device_id: {"unit_id": 1},
        },
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



def test_device_owns_single_endpoint_and_options_stay_outside_domain() -> None:
    snapshot = _snapshot()
    device = snapshot.devices["wt01"]

    assert device.endpoint == ConnectionEndpoint("10.0.0.1", 502)
    assert not hasattr(device.endpoint, "options")
    assert snapshot.device_options_for(device.device_id) == {"unit_id": 1}


def test_core_config_rejects_noncanonical_unit_instance() -> None:
    snapshot = _snapshot()
    point = next(iter(snapshot.business_points.values()))
    invalid_point = BusinessPoint(
        point.business_point_id,
        point.data_type,
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
        device_options=snapshot.device_options,
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
    current_model = next(iter(current.device_models.values()))
    changed_model = DeviceModel(
        current_model.device_model_id,
        current_model.device_type_id,
        replacement.point_table_id,
        name=current_model.name,
        manufacturer=current_model.manufacturer,
    )
    changed = CoreConfigSnapshot(
        device_types=current.device_types,
        device_models={
            changed_model.device_model_id: changed_model,
        },
        devices=current.devices,
        business_points=current.business_points,
        point_tables={
            **current.point_tables,
            replacement.point_table_id: replacement,
        },
        device_options=current.device_options,
    )

    diff = compute_core_config_diff(current, changed)

    assert diff.changed is True
    assert diff.device_models.updated == ("m1",)


def test_core_config_rejects_unknown_model_point_table() -> None:
    snapshot = _snapshot()
    model = next(iter(snapshot.device_models.values()))
    invalid_model = DeviceModel(
        model.device_model_id,
        model.device_type_id,
        "missing_table",
    )
    invalid = CoreConfigSnapshot(
        device_types=snapshot.device_types,
        device_models={
            invalid_model.device_model_id: invalid_model,
        },
        devices=snapshot.devices,
        business_points=snapshot.business_points,
        point_tables=snapshot.point_tables,
        device_options=snapshot.device_options,
    )

    with pytest.raises(ConfigError, match="unknown point table"):
        validate_core_config(invalid)


def test_yaml_round_trip_preserves_point_extensions() -> None:
    codec = YamlCoreConfigCodec()
    snapshot = _snapshot()
    artifact = codec.encode(snapshot)
    decoded = codec.decode(artifact)

    device = decoded.devices["wt01"]
    point = decoded.point_tables["wt_modbus"].points["p"]

    assert not hasattr(device.endpoint, "options")
    assert decoded.device_options_for(device.device_id) == {"unit_id": 1}
    assert point.ext["address"] == 1
    assert point.ext["data_type"] == "float32"
