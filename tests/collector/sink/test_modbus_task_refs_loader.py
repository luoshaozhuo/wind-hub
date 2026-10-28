"""PR#92 Round5 D3：loader 对 Modbus task_refs 的解析、冻结与热更新形态。

覆盖：
- 显式 ref 与缺省 ref（``device_id.point_id``）解析到所属 Task；
- 同一 source 在两个 Task 中以不同 ref 映射不同寄存器时不被合并身份；
- 跨 Task 重复 ref 在解析期拒绝；
- task_refs 与 task_sources/points 一致冻结；
- 热更新（重新加载）后旧 ref/旧地址消失。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from collector.infrastructure.config.loader import load_collector_config
from core.application import ConfigError
from core.application.sink_contract import ResolvedSink
from core.infrastructure.config.adapter import YamlConfigAdapter
from tests.support.new_collector import write_collector_config_tree

pytestmark = pytest.mark.unit

MODBUS_SINKS = {
    "mb": {
        "type": "modbus",
        "enabled": True,
        "connection": {"host": "127.0.0.1", "port": 15020},
    }
}

POINTS = {
    "p1": {
        "point_groups": ["g"],
        "address": {"register_type": "holding", "address": 100},
        "data_type": "float32",
    },
    "p2": {
        "point_groups": ["g"],
        "address": {"register_type": "holding", "address": 102},
        "data_type": "float32",
    },
    "p3": {
        "point_groups": ["g"],
        "address": {"register_type": "holding", "address": 104},
        "data_type": "float32",
    },
}


def _mb_point(point_id: str, ref: str | None, address: int) -> dict[str, object]:
    raw: dict[str, object] = {
        "source": {"device_id": "dev1", "point_id": point_id},
        "datatype": "float32",
        "address": {
            "slave_id": 1,
            "register_type": "holding",
            "address": address,
        },
    }
    if ref is not None:
        raw["ref"] = ref
    return raw


def _load(tmp_path: Path, tasks: dict[str, object], name: str = "cfg"):
    base = write_collector_config_tree(
        tmp_path / name, sinks=MODBUS_SINKS, tasks=tasks, points=POINTS
    )
    return load_collector_config(YamlConfigAdapter(base))


def _two_tasks(
    a_points: list[dict[str, object]], b_points: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "tA": {
            "device_group_id": "dev1",
            "point_group": "g",
            "interval": 1.0,
            "outputs": [{"sink_id": "mb", "points": a_points}],
        },
        "tB": {
            "device_group_id": "dev1",
            "point_group": "g",
            "interval": 1.0,
            "outputs": [{"sink_id": "mb", "points": b_points}],
        },
    }


def test_explicit_and_default_refs_resolve_per_task(tmp_path: Path) -> None:
    # tA：p1 显式 ref、p2 缺省 ref；tB：p1 以不同 ref 映射另一寄存器，p3 缺省
    tasks = _two_tasks(
        [_mb_point("p1", "a.p1", 100), _mb_point("p2", None, 102)],
        [_mb_point("p1", "b.p1", 110), _mb_point("p3", None, 112)],
    )
    config = _load(tmp_path, tasks)
    sink = config.sinks["mb"]
    assert isinstance(sink, ResolvedSink)

    assert sink.task_sources == {
        "tA": (("dev1", "p1"), ("dev1", "p2")),
        "tB": (("dev1", "p1"), ("dev1", "p3")),
    }
    assert sink.task_refs == {
        "tA": ("a.p1", "dev1.p2"),
        "tB": ("b.p1", "dev1.p3"),
    }
    # 同一 source 的两条映射身份独立，各自占一个寄存器布局
    assert {point["ref"] for point in sink.points} == {
        "a.p1",
        "dev1.p2",
        "b.p1",
        "dev1.p3",
    }


def test_task_refs_frozen_along_with_sources(tmp_path: Path) -> None:
    tasks = _two_tasks([_mb_point("p1", "a.p1", 100)], [_mb_point("p2", "b.p2", 102)])
    sink = _load(tmp_path, tasks).sinks["mb"]
    assert isinstance(sink, ResolvedSink)
    assert sink.task_refs is not None
    with pytest.raises(TypeError):
        sink.task_refs["tC"] = ("x",)  # type: ignore[index]
    with pytest.raises(TypeError):
        sink.task_refs["tA"][0] = "mutated"  # type: ignore[index]


def test_duplicate_ref_across_tasks_rejected(tmp_path: Path) -> None:
    """两个 Task 用相同 ref（即使同源）无法在一个 Sink 内重复声明映射。"""
    tasks = _two_tasks(
        [_mb_point("p1", "shared", 100)],
        [_mb_point("p1", "shared", 110)],
    )
    with pytest.raises(ConfigError, match="duplicate point ref"):
        _load(tmp_path, tasks, name="cfg-dup")


def test_non_modbus_sink_has_no_task_refs(tmp_path: Path) -> None:
    tasks = {
        "tA": {
            "device_group_id": "dev1",
            "point_group": "g",
            "interval": 1.0,
            "outputs": [{"sink_id": "rd", "points": [_redis_point()]}],
        }
    }
    sinks = {"rd": {"type": "redis", "enabled": True, "connection": {"host": "127.0.0.1"}}}
    base = write_collector_config_tree(
        tmp_path / "cfg-redis", sinks=sinks, tasks=tasks, points=POINTS
    )
    sink = load_collector_config(YamlConfigAdapter(base)).sinks["rd"]
    assert isinstance(sink, ResolvedSink)
    assert sink.task_refs is None


def _redis_point() -> dict[str, object]:
    return {
        "source": {"device_id": "dev1", "point_id": "p1"},
        "address": {"field": "dev1_p1"},
    }


def test_reload_removes_stale_refs_and_addresses(tmp_path: Path) -> None:
    tasks_v1 = _two_tasks(
        [_mb_point("p1", "a.p1", 100), _mb_point("p2", None, 102)],
        [_mb_point("p3", "b.p3", 110)],
    )
    first = _load(tmp_path, tasks_v1, name="v1").sinks["mb"]
    assert isinstance(first, ResolvedSink)
    assert first.task_refs == {"tA": ("a.p1", "dev1.p2"), "tB": ("b.p3",)}

    # v2：tA 只保留 p1 并改地址；tB 删除
    tasks_v2: dict[str, object] = {
        "tA": {
            "device_group_id": "dev1",
            "point_group": "g",
            "interval": 1.0,
            "outputs": [{"sink_id": "mb", "points": [_mb_point("p1", "a.p1", 200)]}],
        }
    }
    second = _load(tmp_path, tasks_v2, name="v2").sinks["mb"]
    assert isinstance(second, ResolvedSink)
    assert second.task_refs == {"tA": ("a.p1",)}
    assert second.task_sources == {"tA": (("dev1", "p1"),)}
    assert {point["ref"] for point in second.points} == {"a.p1"}
    addresses = {point["address"]["address"] for point in second.points}
    assert addresses == {200}
