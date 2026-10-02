"""ConfigUseCase 的单元测试。

验证对象：``application/usecase/config.py`` 的热重载编排——
「load → validate → diff → Runtime.reconfigure → commit current config」。

覆盖点：

- 构造不触发 ``load_config``——初始快照由组合根注入（单一启动快照）；
- diff 计算（设备/sink/task 增删改、点表变更）——含
  ``diff.tasks: TaskDiff(added/removed/updated/unchanged)``；
- 旧模型字段已移除：``ConfigDiff`` 不再有 ``rules_changed``，
  配置目录不再需要 ``routing.yaml``；
- 非法配置：中止重载、不触碰 Runtime、旧快照保持；
- 无变更：不调用 reconfigure 直接成功；
- 有变更：以 ``(new_config, diff)`` 调用 ``Runtime.reconfigure`` 一次；
- reconfigure 返回错误：``success=False``、错误透传、成功快照不推进；
  下一次 reload 重新计算同一 diff 并重试；
- 点表继承的父表变化向子表传播。

Runtime 用 mock——本层只验证编排，重构执行由
``tests/unit/runtime/test_runtime.py`` 覆盖。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from tests.config_helper import write_config_tree
from wind_hub_collector.application.runtime import Runtime
from wind_hub_collector.application.usecase.config import ConfigUseCase, compute_diff
from wind_hub_core.config.loader import load_config
from wind_hub_core.config.schema import (
    CollectionTaskConfig,
    DeviceConfig,
    PointAddress,
    PointConfig,
    SinkConfig,
    TaskTarget,
)
from wind_hub_core.model.device import Endpoint

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
) -> Path:
    """写出一套自包含配置目录，返回该目录。"""
    raw_devices: list[dict] = []
    for d in devices or []:
        dump = d.model_dump()
        raw: dict = {
            "device_id": dump["device_id"],
            "protocol": dump["protocol"],
            "point_table": dump["point_table"],
            "device_group": dump["device_group"],
            "enabled": dump["enabled"],
            "endpoint": dump["endpoint"],
        }
        if dump["protocol"] == "ads":
            raw["read_mode"] = dump["read_mode"]
        raw_devices.append(raw)
    return write_config_tree(
        base,
        devices=raw_devices,
        point_tables={"t1": {"points": [p.model_dump() for p in (points or [])]}},
        sinks=[s.model_dump() for s in (sinks or [])],
        tasks=[t.model_dump() for t in (tasks or [])],
        system={"runtime": {"queue_maxsize": 10}},
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
        address=PointAddress(type="holding_register", address=1),
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


def _usecase(config_dir: Path, runtime: MagicMock | None = None) -> ConfigUseCase:
    """按装配语义构造 ConfigUseCase——加载一次配置并显式注入快照。"""
    return ConfigUseCase(config_dir, runtime or _mock_runtime(), load_config(config_dir))


# ---------------------------------------------------------------------------
# 初始加载
# ---------------------------------------------------------------------------


async def test_initial_load_exposes_current_config(tmp_path: Path) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")], points=[_make_point()])
    usecase = _usecase(tmp_path, _mock_runtime())

    cfg = usecase.current_config
    assert [d.device_id for d in cfg.devices.devices] == ["d1"]
    assert cfg.tasks.tasks == []


async def test_initial_load_invalid_config_raises(tmp_path: Path) -> None:
    """启动快照由组合根加载——非法配置在加载期（而非用例构造期）暴露。"""
    (tmp_path / "system.yaml").write_text("not: [valid")
    with pytest.raises(Exception):  # noqa: B017 — 加载失败类型由 loader 决定
        _usecase(tmp_path, _mock_runtime())


async def test_init_does_not_load_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ConfigUseCase 构造不触发 load_config——启动快照由组合根注入。"""
    _write_configs(tmp_path, devices=[_make_device("d1")])
    cfg = load_config(tmp_path)

    def _boom(_dir: Path) -> None:
        raise AssertionError("load_config must not be called by ConfigUseCase.__init__")

    monkeypatch.setattr("wind_hub_collector.application.usecase.config.load_config", _boom)
    usecase = ConfigUseCase(tmp_path, _mock_runtime(), cfg)
    assert usecase.current_config is cfg


# ---------------------------------------------------------------------------
# reload —— load / validate 阶段
# ---------------------------------------------------------------------------


async def test_reload_invalid_config_aborts_without_touching_runtime(tmp_path: Path) -> None:
    _write_configs(tmp_path, devices=[_make_device("d1")])
    runtime = _mock_runtime()
    usecase = _usecase(tmp_path, runtime)
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
    usecase = _usecase(tmp_path, runtime)
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
    usecase = _usecase(tmp_path, runtime)

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
    usecase = _usecase(tmp_path, runtime)

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


async def test_reload_partial_failure_keeps_success_baseline_for_retry(tmp_path: Path) -> None:
    """reconfigure 部分失败：成功基线不推进，同一配置下一次继续重试。"""
    _write_configs(tmp_path, devices=[_make_device("d1")])
    runtime = _mock_runtime(reconfigure_errors=["sink: open failed"])
    usecase = _usecase(tmp_path, runtime)
    snapshot_before = usecase.current_config

    _write_configs(tmp_path, devices=[_make_device("d1"), _make_device("d2")])
    result = await usecase.reload()

    assert result.success is False
    assert result.errors == ["sink: open failed"]
    assert usecase.current_config is snapshot_before

    runtime.reconfigure.return_value = []
    result2 = await usecase.reload()

    assert result2.success is True
    assert result2.diff.devices.added == ["d2"]
    assert runtime.reconfigure.await_count == 2
    assert [d.device_id for d in usecase.current_config.devices.devices] == ["d1", "d2"]


async def test_reload_propagates_diff_details(tmp_path: Path) -> None:
    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    runtime = _mock_runtime()
    usecase = _usecase(tmp_path, runtime)

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
    usecase = _usecase(tmp_path, runtime)

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
    old = _usecase(tmp_path, _mock_runtime()).current_config

    _write_configs(
        tmp_path,
        tasks=[
            _make_task("keep"),
            _make_task("change", interval=2.0),  # 内容变化
            _make_task("new"),
        ],
        **base_kwargs,
    )
    new = _usecase(tmp_path, _mock_runtime()).current_config

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
    old = _usecase(tmp_path, _mock_runtime()).current_config

    _write_configs(
        tmp_path,
        devices=[_make_device("d1")],
        points=[_make_point("p2")],
    )
    new = _usecase(tmp_path, _mock_runtime()).current_config

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
    usecase = _usecase(tmp_path)
    usecase2 = _usecase(tmp_path)

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
                    "protocol": "modbus",
                    "points": [
                        {
                            "point_id": "p1",
                            "point_groups": ["g"],
                            "address": {"type": "holding_register", "address": 1},
                            "data_type": "float32",
                            "unit": base_unit,
                        }
                    ]
                },
                "child": {"extends": "base"},
                "other": {
                    "protocol": "modbus",
                    "points": [
                        {
                            "point_id": "p9",
                            "point_groups": ["g"],
                            "address": {"type": "holding_register", "address": 9},
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
    usecase = _usecase(tmp_path, runtime)
    assert usecase.current_config.point_tables.tables["child"].points[0].unit == "rpm"

    _write_inheritance_configs(tmp_path, base_unit="celsius")  # 只改父表
    result = await usecase.reload()

    assert result.success is True
    runtime.reconfigure.assert_awaited_once()
    new_cfg, diff = runtime.reconfigure.await_args.args
    # 父表变化把继承它的子表一并标记为变更——Runtime 据此为绑定子表的
    # 设备重新注入点映射（_reinject_changed_tables 路径）
    assert diff.points_changed is True
    assert diff.point_tables_changed == ["base", "child"]
    child_points = {p.point_id: p for p in new_cfg.point_tables.tables["child"].points}
    assert child_points["p1"].unit == "celsius"
    # 无关表与无关设备不受影响
    assert "other" not in diff.point_tables_changed
    assert diff.devices.unchanged == ["d1", "d2"]
    assert usecase.current_config is new_cfg


async def test_reload_unmodified_inheritance_chain_is_noop(tmp_path: Path) -> None:
    """继承链配置未变：reload 无 diff、不触碰 Runtime。"""
    _write_inheritance_configs(tmp_path)
    runtime = _mock_runtime()
    usecase = _usecase(tmp_path, runtime)

    result = await usecase.reload()

    assert result.success is True
    assert result.diff.has_any_changes is False
    runtime.reconfigure.assert_not_awaited()
