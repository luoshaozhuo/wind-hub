"""Collector 通过 v2 三路径文档快照装配生效配置。

v2 不再有扁平 Core 目录适配：:class:`CollectorDocumentLoader` 从
``services/collector.yaml`` + ``instances/`` + ``templates/`` 读取不可变
模板与设备实例，:func:`load_collector_config` 在其输出上装配生效配置，
并叠加设备文档中持久化的 task desired_state。加载路径必须只读。
"""

from pathlib import Path
from typing import Any

import pytest

from collector.infrastructure.config.loader import load_collector_config
from core.infrastructure.config.collector_documents import (
    CollectorDocumentLoader,
    ConfigPaths,
)
from tests.support.functional_config import MODBUS_POINTS
from tests.support.v2_config import write_v2_component_tree

pytestmark = pytest.mark.unit


def _paths(tree: Path) -> ConfigPaths:
    return ConfigPaths(
        service=tree / "services" / "collector.yaml",
        instances=tree / "instances",
        templates=tree / "templates",
    )


def test_collector_loads_domain_snapshot(tmp_path: Path) -> None:
    tree = write_v2_component_tree(
        tmp_path / "cfg",
        devices={
            "modbus-1": {
                "endpoint": {"host": "127.0.0.1", "port": 5020},
                "overrides": {
                    "protocol_options": {"slave_id": 1},
                    "task": {"desired_state": "START"},
                },
            }
        },
        point_tables={"modbus": {"protocol": "modbus", "points": dict(MODBUS_POINTS)}},
        task_point_group="telemetry",
    )
    loader = CollectorDocumentLoader(_paths(tree))
    snapshot = loader.load_collector()
    config = load_collector_config(loader)

    assert set(config.devices) == {"modbus-1"}
    assert set(config.devices) == set(snapshot.site.devices)
    assert config.device_models == snapshot.device_models
    assert config.point_tables == snapshot.point_tables
    assert config.tasks == snapshot.tasks
    assert config.messaging.servers == tuple(snapshot.system.messaging.servers)
    assert config.messaging.delivery == snapshot.system.messaging.delivery
    # 持久化的 task 意图叠加进生效配置。
    assert config.desired_states["modbus-1"] == "START"


def test_collector_accepts_inmemory_documents_without_writing(tmp_path: Path) -> None:
    tree = write_v2_component_tree(
        tmp_path / "cfg",
        devices={
            "modbus-1": {
                "endpoint": {"host": "127.0.0.1", "port": 5020},
                "overrides": {"protocol_options": {"slave_id": 1}},
            }
        },
        point_tables={"modbus": {"protocol": "modbus", "points": dict(MODBUS_POINTS)}},
        task_point_group="telemetry",
    )
    documents = CollectorDocumentLoader(_paths(tree)).load()
    service_mtime = (tree / "services" / "collector.yaml").stat().st_mtime_ns
    devices_mtime = (tree / "instances" / "devices.yaml").stat().st_mtime_ns

    class Source:
        def load(self) -> Any:
            return documents

        def save(self, value: Any) -> None:
            raise AssertionError("loading must never write")

    config = load_collector_config(Source())  # type: ignore[arg-type]
    assert set(config.devices) == {"modbus-1"}
    assert (tree / "services" / "collector.yaml").stat().st_mtime_ns == service_mtime
    assert (tree / "instances" / "devices.yaml").stat().st_mtime_ns == devices_mtime
