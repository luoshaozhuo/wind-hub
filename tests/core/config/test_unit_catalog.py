"""UnitCatalog 与 units.yaml 解析测试：单位目录由配置驱动。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application import ConfigError
from core.domain.unit import DIMENSIONLESS, Quantity, Unit, UnitCatalog, convert_value
from core.infrastructure.config.units import dump_unit_catalog, parse_unit_catalog
from tests.support.units import MINIMAL_UNITS_DOCUMENT, minimal_unit_catalog

TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "configs"


def _catalog(*units: Unit, quantities: dict[str, str] | None = None) -> UnitCatalog:
    return UnitCatalog.build(
        quantities=quantities or {"dimensionless": "none", "active_power": "watt"},
        units=units,
    )


# ---- 目录构建与校验 ----


def test_catalog_builds_from_definitions() -> None:
    catalog = minimal_unit_catalog()
    assert catalog.unit("kilowatt").symbol == "kW"
    assert catalog.base_unit("active_power").unit_id == "watt"
    assert "temperature" in catalog.quantity_ids()


def test_unit_must_reference_defined_quantity() -> None:
    with pytest.raises(ValueError, match="undefined quantity"):
        _catalog(Unit("x", "x", Quantity("unknown")))


def test_base_unit_must_be_defined() -> None:
    with pytest.raises(ValueError, match="not defined"):
        UnitCatalog.build(
            quantities={"active_power": "missing"},
            units=[Unit("watt", "W", Quantity("active_power"))],
        )


def test_base_unit_must_belong_to_quantity() -> None:
    with pytest.raises(ValueError, match="belongs to"):
        UnitCatalog.build(
            quantities={"dimensionless": "none", "active_power": "none"},
            units=[
                Unit("none", "", DIMENSIONLESS),
                Unit("watt", "W", Quantity("active_power")),
            ],
        )


def test_base_unit_must_be_identity_transform() -> None:
    with pytest.raises(ValueError, match="scale_to_base=1"):
        UnitCatalog.build(
            quantities={"active_power": "kilowatt"},
            units=[
                Unit("watt", "W", Quantity("active_power")),
                Unit("kilowatt", "kW", Quantity("active_power"), scale_to_base=1000.0),
            ],
        )


def test_duplicate_unit_id_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate unit ID"):
        _catalog(
            Unit("watt", "W", Quantity("active_power")),
            Unit("watt", "W2", Quantity("active_power")),
        )


def test_invalid_scale_and_offset_rejected() -> None:
    with pytest.raises(ValueError, match="scale_to_base"):
        Unit("bad", "x", Quantity("active_power"), scale_to_base=0.0)
    with pytest.raises(ValueError, match="scale_to_base"):
        Unit("bad", "x", Quantity("active_power"), scale_to_base=float("nan"))
    with pytest.raises(ValueError, match="offset_to_base"):
        Unit("bad", "x", Quantity("active_power"), offset_to_base=float("inf"))


def test_undefined_unit_lookup_rejected() -> None:
    with pytest.raises(ValueError, match="undefined unit ID"):
        minimal_unit_catalog().unit("not-a-unit")


def test_undefined_quantity_base_lookup_rejected() -> None:
    with pytest.raises(ValueError, match="undefined quantity ID"):
        minimal_unit_catalog().base_unit("not-a-quantity")


# ---- 单位换算 ----


def test_celsius_kelvin_conversion() -> None:
    catalog = minimal_unit_catalog()
    assert catalog.convert(25.0, "celsius", "kelvin") == pytest.approx(298.15)
    assert catalog.convert(298.15, "kelvin", "celsius") == pytest.approx(25.0)


def test_power_prefix_conversion() -> None:
    catalog = minimal_unit_catalog()
    assert catalog.convert(1.5, "kilowatt", "watt") == pytest.approx(1500.0)
    assert catalog.convert(2.0, "megawatt", "kilowatt") == pytest.approx(2000.0)


def test_rpm_radian_per_second_conversion() -> None:
    catalog = minimal_unit_catalog()
    assert catalog.convert(30.0, "rpm", "radian_per_second") == pytest.approx(3.141592653589793)


def test_percent_conversion() -> None:
    catalog = minimal_unit_catalog()
    assert catalog.convert(50.0, "percent", "none") == pytest.approx(0.5)


def test_cross_quantity_conversion_rejected() -> None:
    source = minimal_unit_catalog().unit("kilowatt")
    target = minimal_unit_catalog().unit("hertz")
    with pytest.raises(ValueError, match="incompatible unit quantities"):
        convert_value(1.0, source, target)


# ---- units.yaml 解析与往返 ----


def test_parse_units_document_round_trip() -> None:
    catalog = parse_unit_catalog(MINIMAL_UNITS_DOCUMENT)
    restored = parse_unit_catalog(dump_unit_catalog(catalog))
    assert restored == catalog


def test_parse_rejects_unknown_fields() -> None:
    document = {
        "quantities": {"dimensionless": {"base_unit": "none"}},
        "units": {"none": {"symbol": "", "quantity": "dimensionless", "name": "None"}},
    }
    with pytest.raises(ConfigError, match="unknown keys"):
        parse_unit_catalog(document)


def test_parse_rejects_missing_sections() -> None:
    with pytest.raises(ConfigError, match="quantities"):
        parse_unit_catalog({"units": {"none": {"symbol": "", "quantity": "dimensionless"}}})
    with pytest.raises(ConfigError, match="units"):
        parse_unit_catalog({"quantities": {"dimensionless": {"base_unit": "none"}}})


def test_template_units_yaml_is_complete_catalog() -> None:
    from core.infrastructure.config.yaml import read_yaml_mapping

    catalog = parse_unit_catalog(read_yaml_mapping(TEMPLATE_DIR / "units.yaml"))
    # 迁移的内置单位必须全部存在
    for unit_id in (
        "none",
        "percent",
        "volt",
        "kilovolt",
        "ampere",
        "watt",
        "kilowatt",
        "megawatt",
        "var",
        "kilovar",
        "megavar",
        "volt_ampere",
        "kilovolt_ampere",
        "megavolt_ampere",
        "hertz",
        "hertz_per_second",
        "watt_hour",
        "kilowatt_hour",
        "megawatt_hour",
        "meter",
        "millimeter",
        "meter_per_second",
        "meter_per_second_squared",
        "standard_gravity",
        "radian",
        "degree",
        "radian_per_second",
        "degree_per_second",
        "rpm",
        "radian_per_second_squared",
        "degree_per_second_squared",
        "rpm_per_second",
        "newton",
        "kilonewton",
        "newton_meter",
        "kilonewton_meter",
        "pascal",
        "kilopascal",
        "megapascal",
        "bar",
        "kelvin",
        "celsius",
        "second",
        "millisecond",
        "minute",
        "hour",
        "ohm",
        "siemens",
        "farad",
        "henry",
    ):
        assert unit_id in catalog, unit_id
    # 风光储缺口补充
    for unit_id in (
        "watt_per_square_meter",
        "kilowatt_hour_per_square_meter",
        "ampere_hour",
        "kilogram_per_cubic_meter",
        "percent_rh",
    ):
        assert unit_id in catalog, unit_id
