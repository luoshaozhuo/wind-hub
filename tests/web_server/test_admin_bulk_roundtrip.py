"""Admin 保存不应删除 Core 未显示的配置字段。"""

from unittest.mock import AsyncMock

import pytest

from web_server.application.config.admin_state import AdminDefinitionsState, AdminStateService

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_bulk_preserves_unit_quantity_and_model_options() -> None:
    admin = AsyncMock()
    state = AdminDefinitionsState(
        units={"kW": {"symbol": "kilowatt"}},
        device_types={"turbine": {"name": "Turbine"}},
        device_models={"m": {"device_type_id": "turbine"}},
        point_tables={},
    )
    await AdminStateService(admin).replace_all([], [], [], state)
    update = admin.mutate_yaml_files.call_args.args[1]
    data = {
        "devices.yaml": {"device_groups": {}, "devices": {}},
        "tasks.yaml": {"tasks": {}},
        "sinks.yaml": {"sinks": {}},
        "points.yaml": {"point_tables": {}},
        "units.yaml": {
            "quantities": {"power": {"base_unit": "W"}},
            "units": {"kW": {"quantity": "power", "scale_to_base": 1000}},
        },
        "device_models.yaml": {
            "device_types": {"turbine": {"description": "generator"}},
            "device_models": {"m": {"protocol_options": {"timeout": 1}}},
        },
    }
    update(data)
    assert data["units.yaml"]["units"]["kW"]["quantity"] == "power"
    assert data["units.yaml"]["units"]["kW"]["scale_to_base"] == 1000
    assert data["device_models.yaml"]["device_models"]["m"]["protocol_options"] == {"timeout": 1}
    assert data["device_models.yaml"]["device_types"]["turbine"]["description"] == "generator"
