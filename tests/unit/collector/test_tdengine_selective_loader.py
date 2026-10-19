"""PR#92 Round4 D1：TDengine Task output 显式 points 的解析期白名单与热更新形态。"""

from pathlib import Path

from collector.infrastructure.config.loader import load_collector_config
from core.application.sink_contract import ResolvedSink
from core.infrastructure.config.adapter import YamlConfigAdapter
from tests.support.new_collector import write_collector_config_tree

TDENGINE_SINKS = {
    "td1": {
        "type": "tdengine",
        "enabled": True,
        "connection": {
            "host": "127.0.0.1",
            "port": 6041,
            "database": "wind_hub",
            "stable": "point_values",
            "username": "root",
            "password": "pw",
        },
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
}


def _source(point_id: str) -> dict[str, object]:
    return {
        "source": {"device_id": "dev1", "point_id": point_id},
        "ref": f"dev1.{point_id}",
        "address": {"field": f"dev1_{point_id}"},
    }


def _load(tmp_path: Path, points: list[dict[str, object]] | None):
    tasks = {
        "t1": {
            "device_group_id": "dev1",
            "point_group": "g",
            "interval": 1.0,
            "outputs": [{"sink_id": "td1", "points": points}],
        }
    }
    base = write_collector_config_tree(
        tmp_path / "cfg", sinks=TDENGINE_SINKS, tasks=tasks, points=POINTS
    )
    return load_collector_config(YamlConfigAdapter(base))


def test_explicit_points_become_task_whitelist(tmp_path: Path) -> None:
    config = _load(tmp_path, [_source("p1")])
    sink = config.sinks["td1"]
    assert isinstance(sink, ResolvedSink)
    assert sink.task_sources == {"t1": (("dev1", "p1"),)}


def test_empty_points_mean_full_task_group(tmp_path: Path) -> None:
    config = _load(tmp_path, [])
    sink = config.sinks["td1"]
    assert isinstance(sink, ResolvedSink)
    assert sink.task_sources == {"t1": (("dev1", "p1"), ("dev1", "p2"))}


def test_reloaded_config_reflects_changed_mapping(tmp_path: Path) -> None:
    first = _load(tmp_path, [_source("p1")])
    assert first.sinks["td1"].task_sources == {"t1": (("dev1", "p1"),)}
    second = _load(tmp_path, [_source("p1"), _source("p2")])
    assert second.sinks["td1"].task_sources == {"t1": (("dev1", "p1"), ("dev1", "p2"))}


def test_output_points_must_be_collected_by_same_task(tmp_path: Path) -> None:
    """映射任务组之外的点必须在解析期 fail-closed（D2 Modbus 同型边界）。"""
    from core.application.errors import ConfigError

    foreign = dict(POINTS)
    foreign["p9"] = {
        "point_groups": ["g2"],
        "address": {"register_type": "holding", "address": 110},
        "data_type": "float32",
    }
    tasks = {
        "t1": {
            "device_group_id": "dev1",
            "point_group": "g",
            "interval": 1.0,
            "outputs": [{"sink_id": "td1", "points": [_source("p9")]}],
        }
    }
    base = write_collector_config_tree(
        tmp_path / "cfg2", sinks=TDENGINE_SINKS, tasks=tasks, points=foreign
    )
    try:
        load_collector_config(YamlConfigAdapter(base))
    except ConfigError as exc:
        assert "point group mismatch" in str(exc)
    else:
        raise AssertionError("expected ConfigError for out-of-group output point")
