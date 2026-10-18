"""AdminStateService 原子配置写测试。"""

from unittest.mock import AsyncMock, MagicMock

from web_server.application.config.admin_state import (
    AdminDefinitionsState,
    AdminDeviceItem,
    AdminSinkItem,
    AdminStateService,
    AdminTaskItem,
)
from web_server.application.config.files import ConfigApplyResult


async def test_replace_all_uses_one_multifile_apply() -> None:
    admin = MagicMock()
    admin.mutate_yaml_files = AsyncMock(return_value=ConfigApplyResult(success=True, revision=7))
    service = AdminStateService(admin)

    result = await service.replace_all(
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
                device_group="g1",
                point_group="fast",
                interval=1.0,
                sinks=["s1"],
            )
        ],
        sinks=[
            AdminSinkItem(
                name="s1",
                type="file",
                connection={"path": "/tmp/out"},
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
                    "points": {},
                }
            },
        ),
    )

    assert result.success is True
    names, mutator = admin.mutate_yaml_files.await_args.args[:2]
    assert set(names) == {
        "devices.yaml",
        "tasks.yaml",
        "sinks.yaml",
        "units.yaml",
        "device_models.yaml",
        "points.yaml",
    }
    documents = {name: {} for name in names}
    mutator(documents)
    assert documents["sinks.yaml"]["sinks"]["s1"]["connection"] == {"path": "/tmp/out"}
    assert documents["devices.yaml"]["devices"]["d1"]["model_id"] == "m1"
    assert documents["devices.yaml"]["devices"]["d1"]["device_group_ids"] == ["g1"]
    assert documents["tasks.yaml"]["tasks"]["t1"]["device_group_id"] == "g1"
    assert documents["tasks.yaml"]["tasks"]["t1"]["outputs"] == [{"sink_id": "s1"}]
    assert admin.mutate_yaml_files.await_count == 1


async def test_replace_all_rejects_device_bound_task() -> None:
    """当前 Core Schema 下 Task 只能绑定 device_group，拒绝 device 直绑。"""
    admin = MagicMock()
    admin.mutate_yaml_files = AsyncMock(return_value=ConfigApplyResult(success=True, revision=1))
    service = AdminStateService(admin)
    names_mutator: list = []

    async def _capture(names, mutate, **kwargs):  # noqa: ANN001, ANN202
        names_mutator.append((names, mutate))
        return ConfigApplyResult(success=True, revision=1)

    admin.mutate_yaml_files = _capture
    result = await service.replace_all(
        devices=[],
        tasks=[
            AdminTaskItem(
                task_id="t1",
                device="d1",
                point_group="fast",
                interval=1.0,
                sinks=["s1"],
            )
        ],
        sinks=[],
        definitions=AdminDefinitionsState(
            units={}, device_types={}, device_models={}, point_tables={}
        ),
    )

    assert result.success is True
    _, mutate = names_mutator[0]
    documents = {name: {} for name in names_mutator[0][0]}
    try:
        mutate(documents)
    except ValueError as exc:
        assert "device_group" in str(exc)
    else:  # pragma: no cover - 必须拒绝
        raise AssertionError("device-bound task must be rejected")
