"""Admin 批量保存时不能破坏 Core 正式单位目录。"""

from web_server.application.config.admin_state import (
    AdminDefinitionsState,
    AdminStateService,
)


def test_definition_documents_do_not_define_quantities() -> None:
    state = AdminDefinitionsState(
        units={"kW": {"symbol": "kW"}},
        device_types={},
        device_models={},
        point_tables={},
    )
    documents = AdminStateService._definition_documents(state)
    assert documents["units.yaml"] == {"units": state.units}


def test_quantities_and_other_roots_survive_bulk_merge() -> None:
    """复现 replace_all 的单位配置合并规则，避免将物理量定义清空。"""
    previous = {
        "quantities": {"power": {"base_unit": "W"}},
        "units": {"old": {"symbol": "old"}},
    }
    new_units = {"kW": {"symbol": "kW"}}
    result = {**previous, "units": new_units}
    assert result["quantities"] is previous["quantities"]
    assert result["units"] == new_units
