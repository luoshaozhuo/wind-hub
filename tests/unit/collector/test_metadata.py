"""CollectorMetadataService 单元测试：测点元数据与采集路由计算。

只构造内存 CollectorConfig，不启动 Runtime；通过最小 stub 提供
active_snapshot，验证纯配置查询语义。
"""

from __future__ import annotations

from typing import cast

import pytest

from collector.application.config import CollectorConfig
from collector.application.config_service import CollectorConfigService
from collector.application.metadata import CollectorMetadataService
from core.application.sink_contract import ResolvedSink
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
    Sink,
    Task,
)
from tests.support.units import NONE_UNIT, minimal_unit_catalog


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
    sinks: dict[str, Sink] | None = None,
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
        sinks=sinks or {},
        disabled_devices=disabled,
    )


def _metadata(config: CollectorConfig) -> CollectorMetadataService:
    return CollectorMetadataService(cast(CollectorConfigService, _StaticConfigService(config)))


def _task(
    task_id: str,
    *,
    group: str = "dev1",
    point_group: str = "g",
    sink_ids: tuple[str, ...] = ("s1",),
    enabled: bool = True,
) -> Task:
    return Task(
        task_id=task_id,
        device_group_id=DeviceGroupId(group),
        point_group=point_group,
        sink_ids=sink_ids,
        interval=1.0,
        enabled=enabled,
    )


def _sink(
    sink_id: str,
    *,
    kind: str = "redis",
    enabled: bool = True,
    points: tuple[dict[str, object], ...] = (),
    task_sources: dict[str, tuple[tuple[str, str], ...]] | None = None,
) -> Sink:
    # 生产装配期 CollectorConfig.sinks 承载的是 ResolvedSink（携带解析期点）；
    # 元数据发布路由据此判断 Modbus Sink 是否显式映射某点。Redis/TDengine 的
    # 选择性输出则依据 loader 填充的 task_sources 白名单。
    connection = (
        {"host": "127.0.0.1", "port": 1502}
        if kind == "modbus"
        else {"host": "127.0.0.1", "port": 6379}
    )
    return ResolvedSink(
        sink_id=sink_id,
        kind=kind,
        enabled=enabled,
        connection=connection,
        points=points,
        task_sources=task_sources,
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


def test_disabled_task_and_disabled_sink_are_not_reported() -> None:
    config = _config(
        tasks={
            "t_off": _task("t_off", enabled=False),
            "t_on": _task("t_on", sink_ids=("s_off", "s_on")),
        },
        sinks={
            "s_off": _sink(
                "s_off",
                enabled=False,
                task_sources={"t_on": (("dev1", "p1"),)},
            ),
            "s_on": _sink("s_on", task_sources={"t_on": (("dev1", "p1"),)}),
        },
    )
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.task_ids == ("t_on",)
    assert row.metadata.sink_ids == ("s_on",)


def test_task_must_match_device_group_and_point_group() -> None:
    config = _config(
        tasks={
            "wrong_device": _task("wrong_device", group="other"),
            "wrong_group": _task("wrong_group", point_group="h"),
            "match": _task("match"),
        },
        sinks={"s1": _sink("s1")},
    )
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.task_ids == ("match",)


def test_point_without_task_coverage_reports_no_publish_path() -> None:
    config = _config(tasks={}, sinks={"s1": _sink("s1")})
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.task_ids == ()
    assert row.metadata.sink_ids == ()


def test_modbus_sink_requires_explicit_point_mapping() -> None:
    mapped = {
        "source": {"device_id": "dev1", "point_id": "p1"},
        "ref": "dev1.p1",
        "address": {"slave_id": 1, "register_type": "holding", "address": 10},
    }
    other = {
        "source": {"device_id": "dev1", "point_id": "p2"},
        "ref": "dev1.p2",
        "address": {"slave_id": 1, "register_type": "holding", "address": 20},
    }
    config = _config(
        tasks={"t1": _task("t1", sink_ids=("m_mapped", "m_unmapped", "f1"))},
        sinks={
            "m_mapped": _sink("m_mapped", kind="modbus", points=(mapped, other)),
            "m_unmapped": _sink("m_unmapped", kind="modbus", points=(other,)),
            "f1": _sink(
                "f1",
                kind="redis",
                task_sources={"t1": (("dev1", "p1"),)},
            ),
        },
    )
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.sink_ids == ("f1", "m_mapped")


def test_multiple_tasks_and_sinks_deduplicate() -> None:
    config = _config(
        tasks={
            "t1": _task("t1", sink_ids=("s1", "s2")),
            "t2": _task("t2", sink_ids=("s2",)),
        },
        sinks={
            "s1": _sink("s1", task_sources={"t1": (("dev1", "p1"),)}),
            "s2": _sink(
                "s2",
                task_sources={
                    "t1": (("dev1", "p1"),),
                    "t2": (("dev1", "p1"),),
                },
            ),
        },
    )
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.task_ids == ("t1", "t2")
    assert row.metadata.sink_ids == ("s1", "s2")


def test_redis_tdengine_report_selected_task_outputs_only() -> None:
    """D2：Redis/TDengine 仅在点属于当前 Task 的 output 白名单时上报为输出。"""
    p1: tuple[tuple[str, str], ...] = (("dev1", "p1"),)
    config = _config(
        tasks={
            "t_select": _task("t_select", sink_ids=("redis_in", "td_out", "shared")),
            "t_other": _task("t_other", sink_ids=("shared",)),
        },
        sinks={
            # 当前 Task 显式选择 p1
            "redis_in": _sink("redis_in", task_sources={"t_select": p1}),
            # Task 路由到该 Sink，但白名单不含 p1（选择性输出）
            "td_out": _sink("td_out", kind="tdengine", task_sources={"t_select": ()}),
            # 多 Task 共享：仅 t_select 选择 p1，仍只上报一次
            "shared": _sink(
                "shared",
                task_sources={"t_select": p1, "t_other": ()},
            ),
        },
    )
    row = _metadata(config).get_points_metadata("dev1", ["p1"]).points[0]
    assert row.metadata is not None
    assert row.metadata.task_ids == ("t_other", "t_select")
    assert row.metadata.sink_ids == ("redis_in", "shared")


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
        sinks={"s1": _sink("s1")},
    )
    request = [f"p{i}" for i in range(200)] + ["missing"] + ["p0"] * 3
    result = _metadata(config).get_points_metadata("dev1", request)
    assert [row.point_id for row in result.points] == request
    assert all(row.metadata is not None for row in result.points[:200])
    assert result.points[200].metadata is None
    assert all(row.metadata is not None for row in result.points[201:])
