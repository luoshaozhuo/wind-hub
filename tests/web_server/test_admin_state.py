"""AdminStateService 原子配置写测试。"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from web_server.application.config.admin_state import (
    AdminDefinitionsState,
    AdminDeviceItem,
    AdminSinkItem,
    AdminStateService,
    AdminTaskItem,
)
from web_server.application.config.files import ConfigApplyResult

pytestmark = pytest.mark.unit


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
                type="redis",
                connection={"host": "127.0.0.1"},
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
    assert documents["sinks.yaml"]["sinks"]["s1"]["connection"] == {"host": "127.0.0.1"}
    assert documents["devices.yaml"]["devices"]["d1"]["model_id"] == "m1"
    assert documents["devices.yaml"]["devices"]["d1"]["device_group_ids"] == ["g1"]
    assert documents["tasks.yaml"]["tasks"]["t1"]["device_group_id"] == "g1"
    # 新契约：Task 只声明采集定义，不再内嵌 Sink 去向。
    assert "outputs" not in documents["tasks.yaml"]["tasks"]["t1"]
    # Sink 绑定以 task_id 为键落在 sinks.yaml.sinks.<id>.outputs；全新保存时
    # 无既有绑定可保留，故为空映射（结构化表单不编辑绑定细节）。
    assert documents["sinks.yaml"]["sinks"]["s1"]["outputs"] == {}
    assert admin.mutate_yaml_files.await_count == 1


async def test_replace_all_preserves_sink_outputs_when_sink_set_changes() -> None:
    """结构化保存不编辑绑定：sinks.yaml 既有 outputs 的 points/key_prefix 必须无损保留。

    模拟两次连续保存共享同一 documents 视图（与 ConfigFileService 事务一致）：
    第一次保存后手工补齐 s1/s2 的 outputs 细节（来自 YAML 高级编辑），第二次
    保存调整 sink 集合与顺序 → 保留项细节无损、删除项消失、新增项裸 outputs。
    """
    admin = MagicMock()
    admin.mutate_yaml_files = AsyncMock(return_value=ConfigApplyResult(success=True))
    service = AdminStateService(admin)
    documents: dict[str, dict[str, object]] = {
        "devices.yaml": {},
        "tasks.yaml": {},
        "sinks.yaml": {},
        "units.yaml": {},
        "device_models.yaml": {},
        "points.yaml": {},
    }
    admin.mutate_yaml_files.side_effect = lambda names, mutator, **kwargs: (
        mutator(documents) or ConfigApplyResult(success=True)
    )

    async def _save(sinks: list[str]) -> None:
        await service.replace_all(
            devices=[],
            tasks=[
                AdminTaskItem(
                    task_id="t1",
                    device_group="g1",
                    point_group="fast",
                    interval=1.0,
                    sinks=sinks,
                )
            ],
            sinks=[AdminSinkItem(name=sink_id, type="redis", connection={}) for sink_id in sinks],
            definitions=AdminDefinitionsState(
                units={}, device_types={}, device_models={}, point_tables={}
            ),
        )

    await _save(["s1", "s2"])
    # 绑定细节以 task_id 为键保存在各 Sink 的 outputs（结构化表单之外的高级编辑）。
    documents["sinks.yaml"]["sinks"]["s1"]["outputs"]["t1"] = {
        "key_prefix": "prefix-1",
        "points": [{"source": {"device_id": "d1", "point_id": "p1"}}],
    }
    documents["sinks.yaml"]["sinks"]["s2"]["outputs"]["t1"] = {
        "points": [{"source": {"device_id": "d1", "point_id": "p2"}}]
    }

    # 第二次保存：删除 s1、新增 s3、顺序变为 s2 在前。
    await _save(["s2", "s3"])

    sinks_doc = documents["sinks.yaml"]["sinks"]
    assert "s1" not in sinks_doc  # 删除项连同绑定消失
    assert sinks_doc["s2"]["outputs"] == {
        "t1": {"points": [{"source": {"device_id": "d1", "point_id": "p2"}}]}
    }
    assert sinks_doc["s3"]["outputs"] == {}  # 新增项裸 outputs


def test_admin_task_item_rejects_device_bound_task() -> None:
    """device 直绑是旧 Schema 死路径：请求边界（模型构造）即拒绝，映射 422。"""
    with pytest.raises(ValidationError) as exc_info:
        AdminTaskItem(
            task_id="t1",
            device="d1",
            point_group="fast",
            interval=1.0,
            sinks=["s1"],
        )
    assert "device_group" in str(exc_info.value)


def test_admin_task_item_requires_group_and_interval() -> None:
    valid = {
        "task_id": "t1",
        "device_group": "g1",
        "point_group": "fast",
        "interval": 1.0,
        "sinks": ["s1"],
    }
    AdminTaskItem(**valid)  # 基线合法

    for mutation in (
        {"device_group": None},
        {"interval": None},
    ):
        with pytest.raises(ValidationError):
            AdminTaskItem(**{**valid, **mutation})

    # 应用层模型不再要求非空 sinks：Task→Sink 绑定归 sinks.yaml.outputs，
    # 结构化表单只做无损保留；“至少一个 Sink”的强制在 inbound 请求模型
    # （AdminStateRequest）层，由 webapi/test_v1.py 覆盖。
    AdminTaskItem(**{**valid, "sinks": []})
