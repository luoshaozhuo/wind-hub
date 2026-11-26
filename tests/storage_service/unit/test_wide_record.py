"""Device-wide Kafka JSON v3 validation and physical column ordering."""

import json

import pytest

from storage_service.domain.quality_bitmap import unpack_quality_bitmap
from storage_service.domain.wide_record import WideDataLevel, WideRecord


def _payload() -> dict[str, object]:
    return {
        "schema_version": 3,
        "wind_farm_id": "farm_a",
        "device_config_id": "device_prod-a_v1.0.1",
        "device_id": "wtg_001",
        "data_level": "protocol",
        "sample_time": "2026-11-25T12:00:00.100000Z",
        "points": {
            "wind_speed": [8.2, 1],
            "rotor_speed": [12.3, 1],
            "active_power": [None, 2],
        },
    }


def _decode(value: dict[str, object], columns: tuple[str, ...]) -> WideRecord:
    return WideRecord.from_kafka_json(
        json.dumps(value).encode("utf-8"), physical_points=columns
    )


def test_record_is_one_complete_device_row_in_physical_column_order() -> None:
    columns = ("active_power", "wind_speed", "rotor_speed")
    row = _decode(_payload(), columns)
    assert row.data_level is WideDataLevel.PROTOCOL
    assert row.point_ids == columns
    assert row.values == (None, 8.2, 12.3)
    assert unpack_quality_bitmap(row.quality_bitmap, 3) == (2, 1, 1)
    assert row.sample_time_us == 1795608000100000


@pytest.mark.parametrize("missing", ["active_power", "wind_speed"])
def test_missing_point_is_not_interpolated(missing: str) -> None:
    payload = _payload()
    points = payload["points"]
    assert isinstance(points, dict)
    del points[missing]
    with pytest.raises(ValueError, match="complete physical point set"):
        _decode(payload, ("active_power", "wind_speed", "rotor_speed"))


def test_unknown_point_is_rejected() -> None:
    payload = _payload()
    points = payload["points"]
    assert isinstance(points, dict)
    points["extraneous"] = [0, 1]
    with pytest.raises(ValueError, match="complete physical point set"):
        _decode(payload, ("active_power", "wind_speed", "rotor_speed"))


def test_duplicate_json_members_are_rejected() -> None:
    wire = (
        b'{"schema_version":3,"schema_version":3,'
        b'"wind_farm_id":"farm","device_config_id":"v","device_id":"d",'
        b'"data_level":"raw","sample_time":"2026-11-25T00:00:00Z",'
        b'"points":{"p":[null,0]}}'
    )
    with pytest.raises(ValueError, match="duplicate JSON member"):
        WideRecord.from_kafka_json(wire, physical_points=("p",))


@pytest.mark.parametrize("value", [[1, 4], [1, True], [float("nan"), 1], [1]])
def test_invalid_point_value_or_quality_is_rejected(value: list[object]) -> None:
    payload = _payload()
    points = payload["points"]
    assert isinstance(points, dict)
    points["wind_speed"] = value
    with pytest.raises(ValueError):
        _decode(payload, ("active_power", "wind_speed", "rotor_speed"))


def test_not_collected_requires_null_value() -> None:
    payload = _payload()
    points = payload["points"]
    assert isinstance(points, dict)
    points["wind_speed"] = [8.2, 0]
    with pytest.raises(ValueError, match="NOT_COLLECTED must have NULL"):
        _decode(payload, ("active_power", "wind_speed", "rotor_speed"))


@pytest.mark.parametrize("version", [1, 2, "3", None])
def test_legacy_point_json_versions_are_never_upgraded(version: object) -> None:
    payload = _payload()
    payload["schema_version"] = version
    with pytest.raises(ValueError, match="Kafka JSON v3 envelope mismatch"):
        _decode(payload, ("active_power", "wind_speed", "rotor_speed"))


@pytest.mark.parametrize("level", ["engineering", "PROTOCOL", ""])
def test_wide_device_level_rejects_legacy_aliases(level: str) -> None:
    payload = _payload()
    payload["data_level"] = level
    with pytest.raises(ValueError, match="invalid data_level"):
        _decode(payload, ("active_power", "wind_speed", "rotor_speed"))
