"""UnitCatalog 与 units.yaml 解析测试：单位目录由配置驱动。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application import ConfigError
from core.domain.unit import DIMENSIONLESS, Quantity, Unit, UnitCatalog, convert_value
from core.infrastructure.config.units import dump_unit_catalog, parse_unit_catalog
from tests.support.units import MINIMAL_UNITS_DOCUMENT, minimal_unit_catalog

TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "configs"


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

    # units 目录现位于 templates/ 并带文件版本头，解析前剥离版本元数据。
    document = {
        key: value
        for key, value in read_yaml_mapping(TEMPLATE_DIR / "templates" / "units.yaml").items()
        if key not in {"schema_version", "file_version"}
    }
    catalog = parse_unit_catalog(document)
    # 单位目录由配置驱动：仓库示例只携带其业务点实际使用的单位，
    # 不再要求内置“完整单位表”。这里钉死示例目录的实际集合与基准换算。
    assert set(catalog.units) == {"none", "watt", "kilowatt"}
    assert catalog.base_units["active_power"] == "watt"
    assert convert_value(1.0, catalog.unit("kilowatt"), catalog.unit("watt")) == 1000.0

    # shipped 业务点引用的每个单位都必须能在目录中解析。
    business = read_yaml_mapping(TEMPLATE_DIR / "templates" / "business_points.yaml")
    for point in business.get("business_points", {}).values():
        assert point["unit"] in catalog.units, point["unit"]
