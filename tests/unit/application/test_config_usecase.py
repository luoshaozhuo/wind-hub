"""ConfigUseCase 的单元测试。

验证对象：``application/usecase/config.py`` 的热重载编排——
「load → validate → diff → Runtime.reconfigure → commit current config」。

覆盖点：

- 初始 load 与 ``current_config``；
- diff 计算（设备/sink/task 增删改、点表变更）——含
  ``diff.tasks: TaskDiff(added/removed/updated/unchanged)``；
- 旧模型字段已移除：``ConfigDiff`` 不再有 ``rules_changed``，
  配置目录不再需要 ``routing.yaml``；
- 非法配置：中止重载、不触碰 Runtime、旧快照保持；
- 无变更：不调用 reconfigure 直接成功；
- 有变更：以 ``(new_config, diff)`` 调用 ``Runtime.reconfigure`` 一次；
- reconfigure 返回错误：``success=False``、错误透传、**快照仍提交**
  （部分失败语义：已应用的变更不回滚，下次 reload 以新快照为基准）；
- 点表继承的父表变化向子表传播。

Runtime 用 mock——本层只验证编排，重构执行由
``tests/unit/runtime/test_runtime.py`` 覆盖。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from wind_hub.application.runtime import Runtime
from wind_hub.application.usecase.config import ConfigUseCase, compute_diff
from wind_hub.config.schema import (
    CollectionTaskConfig,
    DeviceConfig,
    PointAddress,
    PointConfig,
    SinkConfig,
    TaskTarget,
)
from wind_hub.domain.model.device import Endpoint

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_configs(
    base: Path,
    devices: list[DeviceConfig] | None = None,
    sinks: list[SinkConfig] | None = None,
    points: list[PointConfig] | None = None,
    tasks: list[CollectionTaskConfig] | None = None,
) -> None:
    """写出一套完整配置目录（system/devices/points/tasks，无 routing.yaml）。"""
    yaml.safe_dump(
        {
            "runtime": {"queue_maxsize": 10},
            "sinks": [s.model_dump() for s in (sinks or [])],
        },
        (base / "system.yaml").open("w"),
    )
    yaml.safe_dump(
        {"devices": [d.model_dump() for d in (devices or [])]},
        (base / "devices.yaml").open("w"),
    )
    yaml.safe_dump(
        {"point_tables": {"t1": {"points": [p.model_dump() for p in (points or [])]}}},
        (base / "points.yaml").open("w"),
    )
    yaml.safe_dump(
        {"tasks": [t.model_dump() for t in (tasks or [])]},
        (base / "tasks.yaml").open("w"),
    )


def _make_device(device_id: str, protocol: str = "modbus") -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        point_table="t1",
        endpoint=Endpoint(host="10.0.0.1", port=502),
    )


def _make_point(point_id: str = "p1") -> PointConfig:
    return PointConfig(
        point_id=point_id,
        point_groups=["g"],
        address=PointAddress(type="hr"),
    )


def _make_task(
    task_id: str = "task-1",
    device: str = "d1",
    interval: float = 1.0,
    sink: str = "s1",
) -> CollectionTaskConfig:
    return CollectionTaskConfig(
        task_id=task_id,
        device=device,
        point_group="g",
        interval=interval,
        targets=[TaskTarget(sink=sink)],
    )


def _mock_runtime(reconfigure_errors: list[str] | None = None) -> MagicMock:
    runtime = MagicMock(spec=Runtime)
    runtime.reconfigure = AsyncMock(return_value=reconfigure_errors or [])
    return runtime


# ---------------------------------------------------------------------------
# 初始加载
# ---------------------------------------------------------------------------


async def test_initial_load_exposes_current_config(tmp_path: Path) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")], points=[_make_point()])
    usecase = ConfigUseCase(tmp_path, _mock_runtime())

    cfg = usecase.current_config
    assert [d.device_id for d in cfg.devices.devices] == ["d1"]
    assert cfg.tasks.tasks == []


async def test_initial_load_invalid_config_raises(tmp_path: Path) -> None:
    (tmp_path / "system.yaml").write_text("not: [valid")
    with pytest.raises(Exception):  # noqa: B017 — 加载失败类型由 loader 决定
        ConfigUseCase(tmp_path, _mock_runtime())


# ---------------------------------------------------------------------------
# reload —— load / validate 阶段
# ---------------------------------------------------------------------------


async def test_reload_invalid_config_aborts_without_touching_runtime(tmp_path: Path) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")])
    runtime = _mock_runtime()
    usecase = ConfigUseCase(tmp_path, runtime)
    snapshot_before = usecase.current_config

    # 破坏 devices.yaml
    (tmp_path / "devices.yaml").write_text("devices: [broken")
    result = await usecase.reload()

    assert result.success is False
    assert result.errors  # 加载错误如实呈现
    runtime.reconfigure.assert_not_awaited()
    # 旧快照保持——失败的重载不改变 diff 基准
    assert usecase.current_config is snapshot_before


async def test_reload_invalid_task_reference_aborts(tmp_path: Path) -> None:
    """加载期跨文件校验失败（task 引用未知 sink）同样中止重载。"""
    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
        points=[_make_point()],
    )
    runtime = _mock_runtime()
    usecase = ConfigUseCase(tmp_path, runtime)
    snapshot_before = usecase.current_config

    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
        points=[_make_point()],
        tasks=[_make_task(sink="no-such-sink")],
    )
    result = await usecase.reload()

    assert result.success is False
    assert result.errors
    runtime.reconfigure.assert_not_awaited()
    assert usecase.current_config is snapshot_before


# ---------------------------------------------------------------------------
# reload —— diff / no-change
# ---------------------------------------------------------------------------


async def test_reload_no_changes_skips_reconfigure(tmp_path: Path) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")], points=[_make_point()])
    runtime = _mock_runtime()
    usecase = ConfigUseCase(tmp_path, runtime)

    result = await usecase.reload()

    assert result.success is True
    assert result.errors == []
    assert result.diff.has_any_changes is False
    runtime.reconfigure.assert_not_awaited()


# ---------------------------------------------------------------------------
# reload —— reconfigure 调用语义
# ---------------------------------------------------------------------------


async def test_reload_calls_runtime_reconfigure_with_new_config_and_diff(
    tmp_path: Path,
) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")], points=[_make_point()])
    runtime = _mock_runtime()
    usecase = ConfigUseCase(tmp_path, runtime)

    # 新增一台设备
    _write_configs(
        tmp_path,
        devices=[_make_device("d1"), _make_device("d2")],
        points=[_make_point()],
    )
    result = await usecase.reload()

    assert result.success is True
    runtime.reconfigure.assert_awaited_once()
    new_cfg, diff = runtime.reconfigure.await_args.args
    assert diff.devices.added == ["d2"]
    assert [d.device_id for d in new_cfg.devices.devices] == ["d1", "d2"]
    # 成功后提交新快照
    assert usecase.current_config is new_cfg


async def test_reload_commits_snapshot_even_on_partial_failure(tmp_path: Path) -> None:
    """reconfigure 部分失败：success=False、错误透传，但快照仍提交。"""
    _write_configs(tmp_path, devices=[_make_device("d1")])
    runtime = _mock_runtime(reconfigure_errors=["sink: open failed"])
    usecase = ConfigUseCase(tmp_path, runtime)

    _write_configs(tmp_path, devices=[_make_device("d1"), _make_device("d2")])
    result = await usecase.reload()

    assert result.success is False
    assert result.errors == ["sink: open failed"]
    # 快照已提交：再次 reload 同一目录内容时 diff 基准是新快照 → 无变更
    result2 = await usecase.reload()
    assert result2.success is True
    assert result2.diff.has_any_changes is False
    assert runtime.reconfigure.await_count == 1  # 第二轮不再调用


async def test_reload_propagates_diff_details(tmp_path: Path) -> None:
    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    runtime = _mock_runtime()
    usecase = ConfigUseCase(tmp_path, runtime)

    _write_configs(
        tmp_path,
        devices=[_make_device("d2")],  # d1 删除、d2 新增
        sinks=[SinkConfig(name="s1", type="kafka")],  # s1 变更
    )
    result = await usecase.reload()

    assert result.success is True
    assert result.diff.devices.added == ["d2"]
    assert result.diff.devices.removed == ["d1"]
    assert result.diff.sinks.updated == ["s1"]


# ---------------------------------------------------------------------------
# reload —— Task diff 进入 reconfigure
# ---------------------------------------------------------------------------


async def test_reload_task_changes_reach_runtime(tmp_path: Path) -> None:
    """tasks.yaml 的变更经 diff.tasks 传递给 Runtime.reconfigure。"""
    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
        points=[_make_point()],
        tasks=[_make_task("task-1")],
    )
    runtime = _mock_runtime()
    usecase = ConfigUseCase(tmp_path, runtime)

    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
        points=[_make_point()],
        tasks=[_make_task("task-1", interval=2.0), _make_task("task-2")],
    )
    result = await usecase.reload()

    assert result.success is True
    runtime.reconfigure.assert_awaited_once()
    _, diff = runtime.reconfigure.await_args.args
    assert diff.tasks.added == ["task-2"]
    assert diff.tasks.updated == ["task-1"]
    assert diff.tasks.removed == []
    assert diff.tasks.unchanged == []


# ---------------------------------------------------------------------------
# compute_diff 纯函数
# ---------------------------------------------------------------------------


async def test_compute_diff_detects_tasks_added_removed_updated(tmp_path: Path) -> None:
    """Task diff 四分类：added / removed / updated / unchanged。"""
    base_kwargs = {
        "devices": [_make_device("d1")],
        "sinks": [SinkConfig(name="s1", type="file")],
        "points": [_make_point()],
    }
    _write_configs(
        tmp_path,
        tasks=[
            _make_task("keep"),
            _make_task("change", interval=1.0),
            _make_task("drop"),
        ],
        **base_kwargs,
    )
    old = ConfigUseCase(tmp_path, _mock_runtime()).current_config

    _write_configs(
        tmp_path,
        tasks=[
            _make_task("keep"),
            _make_task("change", interval=2.0),  # 内容变化
            _make_task("new"),
        ],
        **base_kwargs,
    )
    new = ConfigUseCase(tmp_path, _mock_runtime()).current_config

    diff = compute_diff(old, new)

    assert diff.tasks.added == ["new"]
    assert diff.tasks.removed == ["drop"]
    assert diff.tasks.updated == ["change"]
    assert diff.tasks.unchanged == ["keep"]
    assert diff.has_any_changes is True
    # 旧 routing 语义已移除——ConfigDiff 不再携带 rules_changed
    assert not hasattr(diff, "rules_changed")


async def test_compute_diff_detects_points_changes(tmp_path: Path) -> None:
    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        points=[_make_point("p1")],
    )
    old = ConfigUseCase(tmp_path, _mock_runtime()).current_config

    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        points=[_make_point("p2")],
    )
    new = ConfigUseCase(tmp_path, _mock_runtime()).current_config

    diff = compute_diff(old, new)

    assert diff.points_changed is True
    assert diff.point_tables_changed == ["t1"]
    assert diff.has_any_changes is True


async def test_compute_diff_identical_configs_report_no_changes(tmp_path: Path) -> None:
    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
        points=[_make_point()],
        tasks=[_make_task("task-1")],
    )
    usecase = ConfigUseCase(tmp_path, _mock_runtime())
    usecase2 = ConfigUseCase(tmp_path, _mock_runtime())

    diff = compute_diff(usecase.current_config, usecase2.current_config)

    assert diff.has_any_changes is False
    assert diff.devices.unchanged == ["d1"]
    assert diff.tasks.unchanged == ["task-1"]


# ---------------------------------------------------------------------------
# 热重载 —— 点表继承的传播
# ---------------------------------------------------------------------------


def _write_inheritance_configs(base: Path, base_unit: str = "rpm") -> None:
    """写出一套含继承链的配置：d1 绑定子表 child（extends base），d2 绑定无关表。"""
    _write_configs(
        base,
        devices=[
            DeviceConfig(
                device_id="d1",
                protocol="modbus",
                point_table="child",
                endpoint=Endpoint(host="10.0.0.1", port=502),
            ),
            DeviceConfig(
                device_id="d2",
                protocol="modbus",
                point_table="other",
                endpoint=Endpoint(host="10.0.0.2", port=502),
            ),
        ],
        points=[_make_point()],  # t1 占位，随即被下方 points.yaml 覆盖
    )
    yaml.safe_dump(
        {
            "point_tables": {
                "base": {
                    "points": [
                        {
                            "point_id": "p1",
                            "point_groups": ["g"],
                            "address": {"type": "hr"},
                            "data_type": "float32",
                            "unit": base_unit,
                        }
                    ]
                },
                "child": {"extends": "base"},
                "other": {
                    "points": [
                        {
                            "point_id": "p9",
                            "point_groups": ["g"],
                            "address": {"type": "hr"},
                            "data_type": "float32",
                        }
                    ]
                },
            }
        },
        (base / "points.yaml").open("w"),
    )


async def test_reload_parent_table_change_propagates_to_child_tables(tmp_path: Path) -> None:
    """父表变化：diff 基于 resolved 结果，子表被标记变更并携带新点集进入 Runtime。"""
    _write_inheritance_configs(tmp_path, base_unit="rpm")
    runtime = _mock_runtime()
    usecase = ConfigUseCase(tmp_path, runtime)
    assert usecase.current_config.point_tables.tables["child"].points[0].unit == "rpm"

    _write_inheritance_configs(tmp_path, base_unit="rps")  # 只改父表
    result = await usecase.reload()

    assert result.success is True
    runtime.reconfigure.assert_awaited_once()
    new_cfg, diff = runtime.reconfigure.await_args.args
    # 父表变化把继承它的子表一并标记为变更——Runtime 据此为绑定子表的
    # 设备重新注入点映射（_reinject_changed_tables 路径）
    assert diff.points_changed is True
    assert diff.point_tables_changed == ["base", "child"]
    child_points = {p.point_id: p for p in new_cfg.point_tables.tables["child"].points}
    assert child_points["p1"].unit == "rps"
    # 无关表与无关设备不受影响
    assert "other" not in diff.point_tables_changed
    assert diff.devices.unchanged == ["d1", "d2"]
    assert usecase.current_config is new_cfg


async def test_reload_unmodified_inheritance_chain_is_noop(tmp_path: Path) -> None:
    """继承链配置未变：reload 无 diff、不触碰 Runtime。"""
    _write_inheritance_configs(tmp_path)
    runtime = _mock_runtime()
    usecase = ConfigUseCase(tmp_path, runtime)

    result = await usecase.reload()

    assert result.success is True
    assert result.diff.has_any_changes is False
    runtime.reconfigure.assert_not_awaited()
