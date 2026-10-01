"""Unit tests for the compute_diff function.

覆盖点：

- devices / sinks：按主键（device_id / sink.name）比较，``model_dump`` 深比较
  分出 added / removed / updated / unchanged；
- tasks：按 task_id 比较，``TaskDiff(added/removed/updated/unchanged)``；
- 点表：新增/删除/内容变化的表名进 ``point_tables_changed``，任一表变化置
  ``points_changed=True``。
"""

from __future__ import annotations

from wind_hub_server.application.usecase.config import compute_diff
from wind_hub.config.schema import (
    CollectionTaskConfig,
    Config,
    DeviceConfig,
    DevicesConfig,
    PointAddress,
    PointConfig,
    ResolvedPointTable,
    ResolvedPointTables,
    SinkConfig,
    SystemConfig,
    TasksConfig,
    TaskTarget,
    UnitConfig,
    UnitsConfig,
)
from wind_hub.domain.model.device import Endpoint


def _make_config(
    devices: list[DeviceConfig] | None = None,
    sinks: list[SinkConfig] | None = None,
    tables: dict[str, ResolvedPointTable] | None = None,
    tasks: list[CollectionTaskConfig] | None = None,
) -> Config:
    return Config(
        system=SystemConfig(
            sinks=sinks or [],
        ),
        units=UnitsConfig(units={"none": UnitConfig(symbol="")}),
        devices=DevicesConfig(devices=devices or []),
        point_tables=ResolvedPointTables(tables=tables or {}),
        tasks=TasksConfig(tasks=tasks or []),
    )


def _ep(host: str = "10.0.0.1") -> Endpoint:
    return Endpoint(host=host, port=502)


def _device(device_id: str, protocol: str = "modbus", **kwargs) -> DeviceConfig:  # type: ignore[no-untyped-def]
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        point_table="t1",
        endpoint=kwargs.pop("endpoint", _ep()),
        **kwargs,
    )


def _table(*points: PointConfig) -> ResolvedPointTable:
    return ResolvedPointTable(protocol="modbus", points=list(points))


def _point(point_id: str = "p1", **kwargs) -> PointConfig:  # type: ignore[no-untyped-def]
    return PointConfig(
        point_id=point_id,
        point_groups=["default"],
        address=PointAddress(type="hr"),
        **kwargs,
    )


def _task(
    task_id: str,
    *,
    device: str | None = "d1",
    device_group: str | None = None,
    point_group: str = "default",
    interval: float = 1.0,
    targets: list[str] | None = None,
    enabled: bool = True,
) -> CollectionTaskConfig:
    return CollectionTaskConfig(
        task_id=task_id,
        device=device,
        device_group=device_group,
        point_group=point_group,
        interval=interval,
        targets=[TaskTarget(sink=s) for s in (targets or ["s1"])],
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# 1. Empty diff — identical configs
# ---------------------------------------------------------------------------


def test_empty_diff_identical_configs() -> None:
    cfg = _make_config(
        devices=[_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
        tables={"t1": _table(_point("p1"))},
        tasks=[_task("task-1")],
    )
    diff = compute_diff(cfg, cfg)
    assert not diff.has_any_changes
    assert diff.devices.added == []
    assert diff.devices.removed == []
    assert diff.devices.updated == []
    assert diff.tasks.added == []
    assert diff.tasks.removed == []
    assert diff.tasks.updated == []
    assert diff.tasks.unchanged == ["task-1"]
    assert not diff.points_changed
    assert diff.point_tables_changed == []


# ---------------------------------------------------------------------------
# 2. Device added
# ---------------------------------------------------------------------------


def test_device_added() -> None:
    old = _make_config(
        devices=[_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    new = _make_config(
        devices=[
            _device("d1"),
            _device("d2", protocol="ads", endpoint=_ep("10.0.0.2")),
        ],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.added == ["d2"]
    assert diff.devices.removed == []
    assert diff.devices.updated == []
    assert diff.devices.unchanged == ["d1"]


# ---------------------------------------------------------------------------
# 3. Device removed
# ---------------------------------------------------------------------------


def test_device_removed() -> None:
    old = _make_config(
        devices=[
            _device("d1"),
            _device("d2", protocol="ads", endpoint=_ep("10.0.0.2")),
        ],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    new = _make_config(
        devices=[_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.removed == ["d2"]
    assert diff.devices.added == []
    assert diff.devices.unchanged == ["d1"]


# ---------------------------------------------------------------------------
# 4. Device updated — endpoint changed
# ---------------------------------------------------------------------------


def test_device_endpoint_changed() -> None:
    old = _make_config(
        devices=[_device("d1", endpoint=_ep("10.0.0.1"))],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    new = _make_config(
        devices=[_device("d1", endpoint=_ep("10.0.1.1"))],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.updated == ["d1"]
    assert diff.devices.added == []
    assert diff.devices.removed == []


# ---------------------------------------------------------------------------
# 5. Device updated — protocol changed
# ---------------------------------------------------------------------------


def test_device_protocol_changed() -> None:
    old = _make_config(
        devices=[_device("d1")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    new = _make_config(
        devices=[_device("d1", protocol="iec104")],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.updated == ["d1"]


# ---------------------------------------------------------------------------
# 6. Device enabled changed
# ---------------------------------------------------------------------------


def test_device_enabled_changed() -> None:
    old = _make_config(
        devices=[_device("d1", enabled=True)],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    new = _make_config(
        devices=[_device("d1", enabled=False)],
        sinks=[SinkConfig(name="s1", type="file")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.updated == ["d1"]


# ---------------------------------------------------------------------------
# 7. Sink added / removed / updated
# ---------------------------------------------------------------------------


def test_sink_added() -> None:
    old = _make_config(sinks=[SinkConfig(name="s1", type="file")])
    new = _make_config(
        sinks=[
            SinkConfig(name="s1", type="file"),
            SinkConfig(name="s2", type="kafka"),
        ],
    )
    diff = compute_diff(old, new)
    assert diff.sinks.added == ["s2"]
    assert diff.sinks.unchanged == ["s1"]


def test_sink_removed() -> None:
    old = _make_config(
        sinks=[
            SinkConfig(name="s1", type="file"),
            SinkConfig(name="s2", type="kafka"),
        ],
    )
    new = _make_config(sinks=[SinkConfig(name="s1", type="file")])
    diff = compute_diff(old, new)
    assert diff.sinks.removed == ["s2"]
    assert diff.sinks.unchanged == ["s1"]


def test_sink_updated() -> None:
    old = _make_config(sinks=[SinkConfig(name="s1", type="file")])
    new = _make_config(sinks=[SinkConfig(name="s1", type="kafka")])
    diff = compute_diff(old, new)
    assert diff.sinks.updated == ["s1"]


# ---------------------------------------------------------------------------
# 8. Task added / removed / updated / unchanged
# ---------------------------------------------------------------------------


def test_task_added() -> None:
    old = _make_config(tasks=[_task("task-1")])
    new = _make_config(tasks=[_task("task-1"), _task("task-2")])
    diff = compute_diff(old, new)
    assert diff.tasks.added == ["task-2"]
    assert diff.tasks.removed == []
    assert diff.tasks.updated == []
    assert diff.tasks.unchanged == ["task-1"]


def test_task_removed() -> None:
    old = _make_config(tasks=[_task("task-1"), _task("task-2")])
    new = _make_config(tasks=[_task("task-1")])
    diff = compute_diff(old, new)
    assert diff.tasks.removed == ["task-2"]
    assert diff.tasks.added == []
    assert diff.tasks.unchanged == ["task-1"]


def test_task_updated_interval_changed() -> None:
    old = _make_config(tasks=[_task("task-1", interval=1.0)])
    new = _make_config(tasks=[_task("task-1", interval=5.0)])
    diff = compute_diff(old, new)
    assert diff.tasks.updated == ["task-1"]
    assert diff.tasks.added == []
    assert diff.tasks.removed == []


def test_task_updated_targets_changed() -> None:
    old = _make_config(tasks=[_task("task-1", targets=["s1"])])
    new = _make_config(tasks=[_task("task-1", targets=["s1", "s2"])])
    diff = compute_diff(old, new)
    assert diff.tasks.updated == ["task-1"]


def test_task_updated_point_group_changed() -> None:
    old = _make_config(tasks=[_task("task-1", point_group="fast")])
    new = _make_config(tasks=[_task("task-1", point_group="slow")])
    diff = compute_diff(old, new)
    assert diff.tasks.updated == ["task-1"]


def test_task_updated_enabled_changed() -> None:
    old = _make_config(tasks=[_task("task-1", enabled=True)])
    new = _make_config(tasks=[_task("task-1", enabled=False)])
    diff = compute_diff(old, new)
    assert diff.tasks.updated == ["task-1"]


def test_task_updated_device_scope_changed() -> None:
    """device ↔ device_group 切换同样体现为 updated（model_dump 深比较）。"""
    old = _make_config(tasks=[_task("task-1", device="d1")])
    new = _make_config(tasks=[_task("task-1", device=None, device_group="turbine")])
    diff = compute_diff(old, new)
    assert diff.tasks.updated == ["task-1"]


def test_tasks_unchanged() -> None:
    cfg = _make_config(tasks=[_task("task-1", interval=2.5, targets=["s1", "s2"])])
    diff = compute_diff(cfg, cfg)
    assert diff.tasks.unchanged == ["task-1"]
    assert diff.tasks.updated == []
    assert not diff.has_any_changes


# ---------------------------------------------------------------------------
# 9. Point tables changed
# ---------------------------------------------------------------------------


def test_table_content_changed() -> None:
    old = _make_config(tables={"t1": _table(_point("p1", data_type="float32"))})
    new = _make_config(tables={"t1": _table(_point("p1", data_type="float32", scale=2.0))})
    diff = compute_diff(old, new)
    assert diff.points_changed
    assert diff.point_tables_changed == ["t1"]


def test_table_added() -> None:
    old = _make_config(tables={"t1": _table(_point("p1"))})
    new = _make_config(tables={"t1": _table(_point("p1")), "t2": _table(_point("p9"))})
    diff = compute_diff(old, new)
    assert diff.points_changed
    assert diff.point_tables_changed == ["t2"]


def test_table_removed() -> None:
    old = _make_config(tables={"t1": _table(_point("p1")), "t2": _table(_point("p9"))})
    new = _make_config(tables={"t1": _table(_point("p1"))})
    diff = compute_diff(old, new)
    assert diff.points_changed
    assert diff.point_tables_changed == ["t2"]


def test_point_added_to_table() -> None:
    old = _make_config(tables={"t1": _table(_point("p1"))})
    new = _make_config(tables={"t1": _table(_point("p1"), _point("p2"))})
    diff = compute_diff(old, new)
    assert diff.points_changed
    assert diff.point_tables_changed == ["t1"]


def test_tables_unchanged() -> None:
    cfg = _make_config(tables={"t1": _table(_point("p1", data_type="float32"))})
    diff = compute_diff(cfg, cfg)
    assert not diff.points_changed
    assert diff.point_tables_changed == []


# ---------------------------------------------------------------------------
# Edge: all diff types at once
# ---------------------------------------------------------------------------


def test_comprehensive_diff() -> None:
    old = _make_config(
        devices=[
            _device("d1", endpoint=_ep("10.0.0.1")),
            _device("d2", protocol="ads", endpoint=_ep("10.0.0.2")),
        ],
        sinks=[
            SinkConfig(name="s1", type="file"),
            SinkConfig(name="s2", type="kafka"),
        ],
        tables={"t1": _table(_point("p1"))},
        tasks=[_task("task-1"), _task("task-2")],
    )
    new = _make_config(
        devices=[
            _device("d1", protocol="iec104", endpoint=_ep("10.0.1.1")),
            _device("d3", endpoint=_ep("10.0.0.3")),
        ],
        sinks=[
            SinkConfig(name="s1", type="kafka"),
            SinkConfig(name="s3", type="db"),
        ],
        tables={"t1": _table(_point("p1"), _point("p2"))},
        tasks=[_task("task-1", interval=9.0), _task("task-3")],
    )
    diff = compute_diff(old, new)
    assert diff.devices.added == ["d3"]
    assert diff.devices.removed == ["d2"]
    assert diff.devices.updated == ["d1"]
    assert diff.sinks.added == ["s3"]
    assert diff.sinks.removed == ["s2"]
    assert diff.sinks.updated == ["s1"]
    assert diff.tasks.added == ["task-3"]
    assert diff.tasks.removed == ["task-2"]
    assert diff.tasks.updated == ["task-1"]
    assert diff.points_changed
    assert diff.point_tables_changed == ["t1"]
    assert diff.has_any_changes


# ---------------------------------------------------------------------------
# DeviceModel 修改 → 引用该型号的全部设备视为 affected
# ---------------------------------------------------------------------------


class TestModelChangeDiff:
    """型号层修改（点表换绑、连接默认值调整）经 resolved DeviceConfig 的深
    比较映射到全部引用设备——热重载保持增量，不需要全量重启。"""

    def _load(self, tmp: str, point_table: str):  # type: ignore[no-untyped-def]
        from pathlib import Path

        from tests.config_helper import write_config_tree
        from wind_hub.config.loader import load_config

        site = write_config_tree(
            Path(tmp),
            devices=[
                {"device_id": "d1", "model": "m1", "endpoint": {"host": "10.0.0.1", "port": 502}},
                {"device_id": "d2", "model": "m1", "endpoint": {"host": "10.0.0.2", "port": 502}},
                {"device_id": "d3", "model": "m2", "endpoint": {"host": "10.0.0.3", "port": 502}},
            ],
            device_models={
                "m1": {"device_type": "turbine", "protocol": "modbus", "point_table": point_table},
                "m2": {"device_type": "turbine", "protocol": "modbus", "point_table": "t2"},
            },
            point_tables={
                "t1": {
                    "points": [
                        {
                            "point_id": "p1",
                            "point_groups": ["g"],
                            "address": {"type": "holding_register", "address": 1},
                        }
                    ]
                },
                "t2": {
                    "points": [
                        {
                            "point_id": "p2",
                            "point_groups": ["g"],
                            "address": {"type": "holding_register", "address": 2},
                        }
                    ]
                },
            },
        )
        return load_config(site)

    def test_model_point_table_change_marks_all_referencing_devices(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        old = self._load(str(tmp_path / "a"), "t1")
        new = self._load(str(tmp_path / "b"), "t2")
        diff = compute_diff(old, new)
        # m1 的点表换绑 → 引用 m1 的 d1/d2 全部 updated；m2 的 d3 不受影响
        assert diff.devices.updated == ["d1", "d2"]
        assert diff.devices.added == []
        assert diff.devices.removed == []
        assert "d3" in diff.devices.unchanged
