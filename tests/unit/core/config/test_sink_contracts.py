"""sinks.yaml 统一 Sink 外部接口契约测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from tests.support.config_helper import write_config_tree
from wind_hub_core.config.diff import compute_diff
from wind_hub_core.config.loader import load_config, load_sinks
from wind_hub_core.config.sinks import SinkConfig
from wind_hub_core.model.errors import ConfigError


def _point() -> dict[str, object]:
    return {
        "point_id": "wind_speed",
        "point_groups": ["all"],
        "address": {"type": "holding_register", "address": 100},
        "data_type": "float32",
        "unit": "meter_per_second",
    }


def _iec104_sink(ioa: int = 40101) -> dict[str, object]:
    return {
        "name": "iec104_scada",
        "type": "iec104",
        "connection": {
            "host": "0.0.0.0",
            "port": 2404,
            "common_address": 1,
        },
        "points": [
            {
                "source": {"device_id": "wt01", "point_id": "wind_speed"},
                "datatype": "float32",
                "unit": "meter_per_second",
                "scale": 1.0,
                "offset": 0.0,
                "address": {"ioa": ioa, "type_id": "M_ME_NC_1"},
            }
        ],
    }


def _site(base: Path, *, contracts: list[dict[str, object]]) -> Path:
    return write_config_tree(
        base,
        devices=[
            {
                "device_id": "wt01",
                "protocol": "modbus",
                "point_table": "t1",
                "endpoint": {"host": "10.0.0.1", "port": 502},
            }
        ],
        point_tables={"t1": {"points": [_point()]}},
        sinks=contracts,
    )


def test_raw_sink_keeps_omitted_ref_unresolved() -> None:
    raw = _iec104_sink()
    raw["points"][0].pop("datatype", None)  # type: ignore[index]
    raw["points"][0].pop("unit", None)  # type: ignore[index]
    cfg = SinkConfig.model_validate(raw)
    assert cfg.points[0].ref is None
    assert cfg.points[0].datatype is None
    assert cfg.points[0].unit is None


def test_load_config_resolves_ref_datatype_and_unit_from_source() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = _iec104_sink()
        raw["points"][0].pop("datatype", None)  # type: ignore[index]
        raw["points"][0].pop("unit", None)  # type: ignore[index]
        cfg = load_config(_site(Path(td), contracts=[raw]))
        point = cfg.sinks.sinks[0].points[0]
        assert point.ref == "wt01.wind_speed"
        assert point.source_data_type == "float32"
        assert point.source_unit == "meter_per_second"
        assert point.datatype == "float32"
        assert point.unit == "meter_per_second"


def test_wrong_connection_type_rejected() -> None:
    raw = _iec104_sink()
    raw["connection"] = {"path": "/tmp/not-iec104.jsonl"}
    with pytest.raises(ConfigError, match="connection"):
        SinkConfig.model_validate(raw)


def test_duplicate_external_address_rejected() -> None:
    raw = _iec104_sink()
    second = {
        **raw["points"][0],  # type: ignore[index]
        "source": {"device_id": "wt01", "point_id": "other"},
        "ref": "wt01.other",
    }
    raw["points"] = [raw["points"][0], second]  # type: ignore[index]
    with pytest.raises(ConfigError, match="duplicate external address"):
        SinkConfig.model_validate(raw)


def test_load_sinks_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "sinks.yaml"
        path.write_text(yaml.safe_dump({"sinks": [_iec104_sink()]}), encoding="utf-8")
        cfg = load_sinks(path)
        assert cfg.sinks[0].points[0].ref is None


def test_unknown_source_device_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = _iec104_sink()
        raw["points"][0]["source"]["device_id"] = "ghost"  # type: ignore[index]
        site = _site(Path(td), contracts=[raw])
        with pytest.raises(ConfigError, match="unknown device 'ghost'"):
            load_config(site)


def test_unknown_source_point_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = _iec104_sink()
        raw["points"][0]["source"]["point_id"] = "ghost"  # type: ignore[index]
        site = _site(Path(td), contracts=[raw])
        with pytest.raises(ConfigError, match="unknown point 'ghost'"):
            load_config(site)


def test_unknown_sink_unit_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = _iec104_sink()
        raw["points"][0]["unit"] = "ghost_unit"  # type: ignore[index]
        site = _site(Path(td), contracts=[raw])
        with pytest.raises(ConfigError, match="unknown unit 'ghost_unit'"):
            load_config(site)


def test_sink_metadata_override_is_resolved_explicitly() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = _iec104_sink()
        raw["points"][0]["ref"] = "wind.speed"  # type: ignore[index]
        raw["points"][0]["datatype"] = "float64"  # type: ignore[index]
        cfg = load_config(_site(Path(td), contracts=[raw]))
        point = cfg.sinks.sinks[0].points[0]
        assert point.ref == "wind.speed"
        assert point.datatype == "float64"


def test_affine_transform_rejects_non_numeric_source() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = _iec104_sink()
        raw["points"][0]["scale"] = 2.0  # type: ignore[index]
        site = write_config_tree(
            Path(td),
            devices=[
                {
                    "device_id": "wt01",
                    "protocol": "modbus",
                    "point_table": "t1",
                    "endpoint": {"host": "10.0.0.1", "port": 502},
                }
            ],
            point_tables={
                "t1": {"points": [{**_point(), "data_type": "str"}]}
            },
            sinks=[raw],
        )
        with pytest.raises(ConfigError, match="scale/offset require numeric source"):
            load_config(site)


def test_inherited_source_metadata_change_updates_sink_diff() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sink = _iec104_sink()
        sink["points"][0].pop("datatype", None)  # type: ignore[index]
        sink["points"][0].pop("unit", None)  # type: ignore[index]

        def make_site(path: Path, data_type: str) -> Path:
            point = _point()
            point["data_type"] = data_type
            return write_config_tree(
                path,
                devices=[
                    {
                        "device_id": "wt01",
                        "protocol": "modbus",
                        "point_table": "t1",
                        "endpoint": {"host": "10.0.0.1", "port": 502},
                    }
                ],
                point_tables={"t1": {"points": [point]}},
                sinks=[sink],
            )

        old = load_config(make_site(root / "old", "float32"))
        new = load_config(make_site(root / "new", "float64"))
        diff = compute_diff(old, new)
        assert diff.sinks.updated == ["iec104_scada"]
        assert old.sinks.sinks[0].points[0].datatype == "float32"
        assert new.sinks.sinks[0].points[0].datatype == "float64"


def test_duplicate_canonical_ref_rejected_after_resolve() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = _iec104_sink()
        second = {
            **raw["points"][0],  # type: ignore[index]
            "address": {"ioa": 40102, "type_id": "M_ME_NC_1"},
        }
        raw["points"] = [raw["points"][0], second]  # type: ignore[index]
        site = _site(Path(td), contracts=[raw])
        with pytest.raises(ConfigError, match="duplicate ref"):
            load_config(site)

def test_iec104_single_point_requires_bool_datatype() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = _iec104_sink()
        raw["points"][0]["address"] = {"ioa": 40101, "type_id": "M_SP_NA_1"}  # type: ignore[index]
        site = _site(Path(td), contracts=[raw])
        with pytest.raises(ConfigError, match="requires datatype 'bool'"):
            load_config(site)


def test_iec104_single_point_accepts_bool_datatype() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = _iec104_sink()
        raw["points"][0]["datatype"] = "bool"  # type: ignore[index]
        raw["points"][0]["address"] = {"ioa": 40101, "type_id": "M_SP_NA_1"}  # type: ignore[index]
        cfg = load_config(_site(Path(td), contracts=[raw]))
        assert cfg.sinks.sinks[0].points[0].datatype == "bool"

def test_contract_change_appears_in_diff() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old_dir = _site(root / "old", contracts=[_iec104_sink(40101)])
        new_dir = _site(root / "new", contracts=[_iec104_sink(40102)])
        diff = compute_diff(load_config(old_dir), load_config(new_dir))
        assert diff.sinks.updated == ["iec104_scada"]
        assert diff.has_any_changes


def _modbus_site(
    base: Path,
    *,
    sink_points: list[dict[str, object]],
    source_points: list[dict[str, object]],
) -> Path:
    return write_config_tree(
        base,
        devices=[
            {
                "device_id": "wt01",
                "protocol": "modbus",
                "point_table": "t1",
                "endpoint": {"host": "10.0.0.1", "port": 502},
            }
        ],
        point_tables={"t1": {"points": source_points}},
        sinks=[
            {
                "name": "modbus_scada",
                "type": "modbus",
                "connection": {"host": "0.0.0.0", "port": 1502},
                "points": sink_points,
            }
        ],
    )


def _source_point(
    point_id: str,
    data_type: str,
    address: int,
) -> dict[str, object]:
    return {
        "point_id": point_id,
        "point_groups": ["all"],
        "address": {"type": "holding_register", "address": address},
        "data_type": data_type,
        "unit": "none",
    }


def _modbus_sink_point(
    point_id: str,
    register_type: str,
    address: int,
    *,
    datatype: str | None = None,
) -> dict[str, object]:
    point: dict[str, object] = {
        "source": {"device_id": "wt01", "point_id": point_id},
        "address": {
            "unit_id": 1,
            "register_type": register_type,
            "address": address,
        },
    }
    if datatype is not None:
        point["datatype"] = datatype
    return point


def test_modbus_bit_register_requires_bool() -> None:
    with tempfile.TemporaryDirectory() as td:
        site = _modbus_site(
            Path(td),
            source_points=[_source_point("p1", "float32", 10)],
            sink_points=[_modbus_sink_point("p1", "coil", 100)],
        )
        with pytest.raises(ConfigError, match="coil requires datatype 'bool'"):
            load_config(site)


def test_modbus_word_register_rejects_str() -> None:
    with tempfile.TemporaryDirectory() as td:
        site = _modbus_site(
            Path(td),
            source_points=[_source_point("p1", "str", 10)],
            sink_points=[_modbus_sink_point("p1", "holding", 100)],
        )
        with pytest.raises(ConfigError, match="does not support datatype 'str'"):
            load_config(site)


def test_modbus_multi_register_ranges_must_not_overlap() -> None:
    with tempfile.TemporaryDirectory() as td:
        site = _modbus_site(
            Path(td),
            source_points=[
                _source_point("p1", "float32", 10),
                _source_point("p2", "uint16", 20),
            ],
            sink_points=[
                _modbus_sink_point("p1", "holding", 100),
                _modbus_sink_point("p2", "holding", 101),
            ],
        )
        with pytest.raises(ConfigError, match="Modbus address overlap"):
            load_config(site)


def test_modbus_adjacent_ranges_are_valid() -> None:
    with tempfile.TemporaryDirectory() as td:
        site = _modbus_site(
            Path(td),
            source_points=[
                _source_point("p1", "float32", 10),
                _source_point("p2", "uint16", 20),
            ],
            sink_points=[
                _modbus_sink_point("p1", "holding", 100),
                _modbus_sink_point("p2", "holding", 102),
            ],
        )
        cfg = load_config(site)
        assert len(cfg.sinks.sinks[0].points) == 2


def test_modbus_register_range_must_fit_address_space() -> None:
    with tempfile.TemporaryDirectory() as td:
        site = _modbus_site(
            Path(td),
            source_points=[_source_point("p1", "float64", 10)],
            sink_points=[_modbus_sink_point("p1", "input", 65533)],
        )
        with pytest.raises(ConfigError, match="exceeds 65535"):
            load_config(site)


def test_modbus_connection_resolves_to_modbus_type() -> None:
    cfg = SinkConfig.model_validate(
        {
            "name": "modbus_scada",
            "type": "modbus",
            "connection": {"host": "0.0.0.0", "port": 502},
            "points": [
                {
                    "source": {"device_id": "wt01", "point_id": "wind_speed"},
                    "datatype": "float32",
                    "unit": "meter_per_second",
                    "address": {
                        "unit_id": 1,
                        "register_type": "holding",
                        "address": 100,
                    },
                }
            ],
        }
    )
    assert type(cfg.connection).__name__ == "ModbusSinkConnection"
