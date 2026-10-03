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


def test_canonical_ref_generated() -> None:
    cfg = SinkConfig.model_validate(_iec104_sink())
    assert cfg.points[0].ref == "wt01.wind_speed"


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
        assert cfg.sinks[0].points[0].ref == "wt01.wind_speed"


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


def test_contract_change_appears_in_diff() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old_dir = _site(root / "old", contracts=[_iec104_sink(40101)])
        new_dir = _site(root / "new", contracts=[_iec104_sink(40102)])
        diff = compute_diff(load_config(old_dir), load_config(new_dir))
        assert diff.sinks.updated == ["iec104_scada"]
        assert diff.has_any_changes


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
