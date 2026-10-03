"""AdminStateUseCase 原子配置写测试。"""

from unittest.mock import AsyncMock, MagicMock

from wind_hub_server.application.usecase.admin_state import (
    AdminDefinitionsState,
    AdminDeviceItem,
    AdminSinkItem,
    AdminStateUseCase,
    AdminTaskItem,
)
from wind_hub_server.application.usecase.config_admin import ConfigApplyResult


async def test_replace_all_uses_one_multifile_apply() -> None:
    admin = MagicMock()
    admin.mutate_yaml_files = AsyncMock(
        return_value=ConfigApplyResult(success=True, revision=7)
    )
    usecase = AdminStateUseCase(admin)

    result = await usecase.replace_all(
        devices=[
            AdminDeviceItem(
                device_id="d1",
                model="m1",
                device_group="g1",
                host="192.0.2.1",
                port=502,
            )
        ],
        tasks=[
            AdminTaskItem(
                task_id="t1",
                device="d1",
                point_group="fast",
                interval=1.0,
                sinks=["s1"],
            )
        ],
        sinks=[
            AdminSinkItem(
                name="s1",
                type="file",
                connection={"path": "/tmp/out.jsonl"},
            )
        ],
        definitions=AdminDefinitionsState(
            units={"none": {"symbol": "", "name": "None"}},
            device_types={"turbine": {"name": "Turbine"}},
            device_models={
                "m1": {
                    "device_type": "turbine",
                    "protocol": "modbus",
                    "point_table": "p1",
                }
            },
            point_tables={
                "p1": {
                    "protocol": "modbus",
                    "points": [],
                }
            },
        ),
    )

    assert result.success is True
    names, mutator = admin.mutate_yaml_files.await_args.args[:2]
    assert set(names) == {
        "devices.yaml",
        "tasks.yaml",
        "system.yaml",
        "sinks.yaml",
        "units.yaml",
        "device_models.yaml",
        "points.yaml",
    }
    documents = {name: {} for name in names}
    documents["system.yaml"] = {"runtime": {"keep": True}}
    mutator(documents)
    assert documents["system.yaml"]["runtime"] == {"keep": True}
    assert documents["sinks.yaml"]["sinks"][0]["name"] == "s1"
    assert documents["devices.yaml"]["devices"][0]["device_id"] == "d1"
    assert documents["tasks.yaml"]["tasks"][0]["task_id"] == "t1"
    assert admin.mutate_yaml_files.await_count == 1
