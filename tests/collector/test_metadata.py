"""CollectorMetadataService 单元测试：测点元数据与采集任务覆盖计算。

只构造内存 CollectorConfig，不启动 Runtime；通过最小 stub 提供
active_snapshot，验证纯配置查询语义。Sink 绑定归属 sink_service，
Collector 元数据只报告采集侧（点元数据 + 覆盖该点的 Task 列表）。
"""

from __future__ import annotations

from typing import cast

import pytest

from collector.application.config import CollectorConfig
from collector.application.config_service import CollectorConfigService
from collector.application.metadata import CollectorMetadataService
from core.application.settings import MessagingSettings
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Device,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    DeviceTypeId,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
    Task,
)
from tests.support.units import NONE_UNIT, minimal_unit_catalog

pytestmark = pytest.mark.unit


def _messaging() -> MessagingSettings:
    return MessagingSettings(
        servers=("nats://127.0.0.1:4222",),
        delivery="realtime",
        max_staleness_ms=500,
    )


class _StaticConfigService:
    """固定快照的 ConfigService stub；元数据查询只读取 active_snapshot。"""

    def __init__(self, config: CollectorConfig, revision: str = "startup", digest: str = "h"):
        self._config = config
        self._revision = revision
        self._digest = digest

    def active_snapshot(self) -> tuple[CollectorConfig, str, str]:
        return self._config, self._revision, self._digest


def _point(
    point_id: str,
    *,
    groups: tuple[str, ...] = ("g",),
    data_type: str | None = "float32",
) -> Point:
    ext: dict[str, str | int] = {"register_type": "holding", "address": 100}
    if data_type is not None:
        ext["data_type"] = data_type
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        scale=2.0,
        offset=1.0,
        ext=ext,
        point_groups=groups,
    )


def _device(device_id: str, *, model_id: str = "mod", group: str | None = None) -> Device:
    return Device(
        device_id=DeviceId(device_id),
        device_model_id=DeviceModelId(model_id),
        endpoint=ConnectionEndpoint(host="127.0.0.1", port=502),
        device_group_ids=(DeviceGroupId(group or device_id),),
    )


def _config(
    *,
    devices: dict[str, Device] | None = None,
    tables: dict[str, PointTable] | None = None,
    tasks: dict[str, Task] | None = None,
    disabled: frozenset[DeviceId] = frozenset(),
) -> CollectorConfig:
    tables = tables or {
        "tab": PointTable(
            point_table_id=PointTableId("tab"),
            protocol=Protocol("modbus"),
            points={"p1": _point("p1")},
        )
    }
    return CollectorConfig(
        units=minimal_unit_catalog(),
        messaging=_messaging(),
        devices={
            DeviceId(key): value for key, value in (devices or {"dev1": _device("dev1")}).items()
        },
        device_models={
            DeviceModelId("mod"): DeviceModel(
                device_model_id=DeviceModelId("mod"),
                device_type_id=DeviceTypeId("turbine"),
                point_table_id=PointTableId("tab"),
            )
        },
        point_tables={PointTableId(key): value for key, value in tables.items()},
        tasks=tasks or {},
        disabled_devices=disabled,
    )


def _metadata(config: CollectorConfig) -> CollectorMetadataService:
    return CollectorMetadataService(cast(CollectorConfigService, _StaticConfigService(config)))


def _task(
    task_id: str,
    *,
    group: str = "dev1",
    point_group: str = "g",
    enabled: bool = True,
) -> Task:
    return Task(
        task_id=task_id,
        device_group_id=DeviceGroupId(group),
        point_group=point_group,
        interval=1.0,
        enabled=enabled,
    )


def test_rejects_empty_device_id_and_empty_point_ids() -> None:
    metadata = _metadata(_config())
    with pytest.raises(ValueError, match="device_id"):
        metadata.get_points_metadata("", ["p1"])
    with pytest.raises(ValueError, match="point_ids"):
        metadata.get_points_metadata("dev1", [])
    with pytest.raises(ValueError, match="point_ids"):
        metadata.get_points_metadata("dev1", ["p1", ""])


def test_unknown_and_disabled_device_are_distinguished() -> None:
    config = _config(disabled=frozenset({DeviceId("dev2")}))
    metadata = _metadata(config)
    with pytest.raises(KeyError, match="unknown device"):
        metadata.get_points_metadata("ghost", ["p1"])
    with pytest.raises(KeyError, match="disabled"):
        metadata.get_points_metadata("dev2", ["p1"])


def test_metadata_fields_and_data_type_from_authoritative_ext() -> None:
    metadata = _metadata(_config())
    row = metadata.get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.access == "read_write"
    assert row.metadata.data_type == "float32"
    assert row.metadata.unit == "none"
    assert row.metadata.scale == 2.0
    assert row.metadata.offset == 1.0
    # data_type 有专属字段，不得混入 configured_address。
    assert row.metadata.configured_address == {"register_type": "holding", "address": 100}


def test_data_type_does_not_fall_back_to_protocol_address_type() -> None:
    point = Point(
        point_id="p1",
        business_point_id=BusinessPointId("p1"),
        source_unit=NONE_UNIT,
        access=PointAccess.READ,
        ext={"type": "M_SP_NA_1", "ioa": 100},
    )
    config = _config(
        tables={
            "tab": PointTable(
                point_table_id=PointTableId("tab"),
                protocol=Protocol("iec104"),
                points={"p1": point},
            )
        }
    )
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.data_type == ""
    assert row.metadata.configured_address == {"type": "M_SP_NA_1", "ioa": 100}


def test_same_point_id_on_different_devices_is_resolved_per_device() -> None:
    devices = {
        "dev1": _device("dev1"),
        "dev2": _device("dev2", model_id="mod2", group="dev2"),
    }
    tables = {
        "tab": PointTable(
            point_table_id=PointTableId("tab"),
            protocol=Protocol("modbus"),
            points={"p1": _point("p1", data_type="float32")},
        ),
        "tab2": PointTable(
            point_table_id=PointTableId("tab2"),
            protocol=Protocol("modbus"),
            points={"p1": _point("p1", data_type="int16")},
        ),
    }
    config = CollectorConfig(
        units=minimal_unit_catalog(),
        messaging=_messaging(),
        devices={DeviceId(key): value for key, value in devices.items()},
        device_models={
            DeviceModelId("mod"): DeviceModel(
                device_model_id=DeviceModelId("mod"),
                device_type_id=DeviceTypeId("turbine"),
                point_table_id=PointTableId("tab"),
            ),
            DeviceModelId("mod2"): DeviceModel(
                device_model_id=DeviceModelId("mod2"),
                device_type_id=DeviceTypeId("turbine"),
                point_table_id=PointTableId("tab2"),
            ),
        },
        point_tables={PointTableId(key): value for key, value in tables.items()},
    )
    metadata = _metadata(config)
    row1 = metadata.get_points_metadata("dev1", ["p1"]).points[0]
    row2 = metadata.get_points_metadata("dev2", ["p1"]).points[0]
    assert row1.metadata is not None and row1.metadata.data_type == "float32"
    assert row2.metadata is not None and row2.metadata.data_type == "int16"


def test_disabled_task_is_not_reported() -> None:
    config = _config(
        tasks={
            "t_off": _task("t_off", enabled=False),
            "t_on": _task("t_on"),
        },
    )
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.task_ids == ("t_on",)


def test_task_must_match_device_group_and_point_group() -> None:
    config = _config(
        tasks={
            "wrong_device": _task("wrong_device", group="other"),
            "wrong_group": _task("wrong_group", point_group="h"),
            "match": _task("match"),
        },
    )
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.task_ids == ("match",)


def test_point_without_task_coverage_reports_no_publish_path() -> None:
    config = _config(tasks={})
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.task_ids == ()


def test_multiple_tasks_deduplicate() -> None:
    config = _config(
        tasks={
            "t1": _task("t1"),
            "t2": _task("t2"),
        },
    )
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.task_ids == ("t1", "t2")


def test_large_batch_preserves_order_and_duplicates() -> None:
    points = {f"p{i}": _point(f"p{i}") for i in range(200)}
    config = _config(
        tables={
            "tab": PointTable(
                point_table_id=PointTableId("tab"),
                protocol=Protocol("modbus"),
                points=points,
            )
        },
        tasks={"t1": _task("t1")},
    )
    request = [f"p{i}" for i in range(200)] + ["missing"] + ["p0"] * 3
    result = _metadata(config).get_points_metadata("dev1", request)
    assert [row.point_id for row in result.points] == request
    assert all(row.metadata is not None for row in result.points[:200])
    assert result.points[200].metadata is None
    assert all(row.metadata is not None for row in result.points[201:])
