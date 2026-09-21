"""Unit tests for pydantic config schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from wind_hub.config.schema import (
    CollectionTaskConfig,
    DeviceConfig,
    DevicesConfig,
    PointAddress,
    PointConfig,
    PointPatch,
    PointTableConfig,
    PointTablesConfig,
    ResolvedPointTable,
    RuntimeConfig,
    SinkConfig,
    SystemConfig,
    TasksConfig,
    TaskTarget,
)
from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ConfigError

# ---------------------------------------------------------------------------
# SystemConfig / RuntimeConfig
# ---------------------------------------------------------------------------


class TestSinkConfig:
    def test_duplicate_sink_names_raises(self) -> None:
        sinks = [
            SinkConfig(name="kafka", type="kafka"),
            SinkConfig(name="kafka", type="kafka"),
        ]
        with pytest.raises(ConfigError, match="Duplicate"):
            SystemConfig(sinks=sinks)

    def test_unique_sink_names_ok(self) -> None:
        sinks = [
            SinkConfig(name="kafka", type="kafka"),
            SinkConfig(name="file", type="file"),
        ]
        cfg = SystemConfig(sinks=sinks)
        assert len(cfg.sinks) == 2

    def test_defaults(self) -> None:
        cfg = SystemConfig()
        assert cfg.runtime.queue_maxsize == 1000
        assert cfg.runtime.backpressure_policy == "drop_old"
        assert cfg.pipeline.processors == []
        assert cfg.sinks == []
        assert cfg.interfaces.api.port == 8080

    def test_no_scheduler_section(self) -> None:
        """scheduler/default_interval/max_concurrent_devices 已移除。"""
        cfg = SystemConfig()
        assert not hasattr(cfg, "scheduler")
        assert not hasattr(cfg.runtime, "default_interval")
        assert not hasattr(cfg.runtime, "max_concurrent_devices")


class TestRuntimeConfig:
    def test_invalid_backpressure_policy_raises(self) -> None:
        with pytest.raises(ConfigError, match="backpressure_policy"):
            RuntimeConfig(backpressure_policy="explode")

    def test_allowed_backpressure_policies(self) -> None:
        for policy in ("drop_old", "drop_new", "block"):
            assert RuntimeConfig(backpressure_policy=policy).backpressure_policy == policy


# ---------------------------------------------------------------------------
# DevicesConfig
# ---------------------------------------------------------------------------


class TestDevicesConfig:
    def test_protocol_whitelist_rejects_bad_value(self) -> None:
        with pytest.raises(ConfigError, match="opcua"):
            DevicesConfig(
                devices=[
                    DeviceConfig(
                        device_id="d1",
                        point_table="t1",
                        protocol="opcua",
                        endpoint=Endpoint(host="10.0.0.1", port=4840),
                    )
                ]
            )

    def test_allowed_protocols_accepted(self) -> None:
        for proto in ("ads", "modbus", "iec104"):
            cfg = DevicesConfig(
                devices=[
                    DeviceConfig(
                        device_id="d1",
                        point_table="t1",
                        protocol=proto,
                        endpoint=Endpoint(host="10.0.0.1", port=502),
                    )
                ]
            )
            assert cfg.devices[0].protocol == proto

    def test_duplicate_device_id_raises(self) -> None:
        ep = Endpoint(host="10.0.0.1", port=502)
        with pytest.raises(ConfigError, match="Duplicate"):
            DevicesConfig(
                devices=[
                    DeviceConfig(device_id="d1", point_table="t1", protocol="modbus", endpoint=ep),
                    DeviceConfig(device_id="d1", point_table="t1", protocol="ads", endpoint=ep),
                ]
            )

    def test_invalid_read_mode_raises(self) -> None:
        with pytest.raises(ConfigError, match="read_mode"):
            DeviceConfig(
                device_id="d1",
                point_table="t1",
                protocol="ads",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                read_mode="batch",
            )

    def test_valid_read_mode_accepted(self) -> None:
        for read_mode in ("sum", "sequential"):
            cfg = DeviceConfig(
                device_id="d1",
                point_table="t1",
                protocol="ads",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                read_mode=read_mode,
            )
            assert cfg.read_mode == read_mode

    def test_mode_field_removed(self) -> None:
        """``mode``（poll/subscribe）已从生产配置移除——多余字段直接报错。"""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            DeviceConfig(
                device_id="d1",
                point_table="t1",
                protocol="ads",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                mode="poll",  # type: ignore[call-arg]
            )

    def test_subscribe_field_removed(self) -> None:
        """``subscribe`` 不再是正式配置 Schema 的一部分。"""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            DeviceConfig(
                device_id="d1",
                point_table="t1",
                protocol="ads",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                subscribe={"enabled": True},  # type: ignore[call-arg]
            )

    def test_polling_field_removed(self) -> None:
        """设备不再携带 polling——采集周期由 tasks.yaml 的 Task 定义。"""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            DeviceConfig(
                device_id="d1",
                point_table="t1",
                protocol="modbus",
                endpoint=Endpoint(host="10.0.0.1", port=502),
                polling=[{"group": "fast", "interval": 1.0}],  # type: ignore[call-arg]
            )

    def test_supports_scheduled_collection(self) -> None:
        ep = Endpoint(host="10.0.0.1", port=502)
        ads_sum = DeviceConfig(
            device_id="d1", point_table="t1", protocol="ads", endpoint=ep, read_mode="sum"
        )
        ads_seq = DeviceConfig(
            device_id="d2",
            point_table="t1",
            protocol="ads",
            endpoint=ep,
            read_mode="sequential",
        )
        modbus = DeviceConfig(device_id="d3", point_table="t1", protocol="modbus", endpoint=ep)
        assert ads_sum.supports_scheduled_collection is True
        assert ads_seq.supports_scheduled_collection is False
        assert modbus.supports_scheduled_collection is True


# ---------------------------------------------------------------------------
# PointConfig / PointPatch / PointTableConfig / ResolvedPointTable
# ---------------------------------------------------------------------------


class TestPointConfig:
    def test_point_groups_required(self) -> None:
        """point_groups 是必填字段。"""
        with pytest.raises(ValidationError):
            PointConfig(  # type: ignore[call-arg]
                point_id="p1", address=PointAddress(type="hr"), data_type="float32"
            )

    def test_point_groups_empty_list_raises(self) -> None:
        with pytest.raises(ConfigError, match="non-empty"):
            PointConfig(
                point_id="p1",
                point_groups=[],
                address=PointAddress(type="hr"),
                data_type="float32",
            )

    def test_point_groups_duplicates_raise(self) -> None:
        with pytest.raises(ConfigError, match="duplicate point_groups"):
            PointConfig(
                point_id="p1",
                point_groups=["fast", "fast"],
                address=PointAddress(type="hr"),
                data_type="float32",
            )

    def test_point_groups_blank_string_raises(self) -> None:
        with pytest.raises(ConfigError, match="non-empty strings"):
            PointConfig(
                point_id="p1",
                point_groups=["  "],
                address=PointAddress(type="hr"),
                data_type="float32",
            )

    def test_multiple_point_groups_ok(self) -> None:
        point = PointConfig(
            point_id="p1",
            point_groups=["fast", "telemetry"],
            address=PointAddress(type="hr"),
            data_type="float32",
        )
        assert point.point_groups == ["fast", "telemetry"]

    def test_group_and_sinks_fields_removed(self) -> None:
        """点不再声明 group / sinks——选点走 point_groups，输出走 Task targets。"""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            PointConfig(
                point_id="p1",
                point_groups=["fast"],
                address=PointAddress(type="hr"),
                group="fast",  # type: ignore[call-arg]
            )
        with pytest.raises(ValidationError, match="extra_forbidden"):
            PointConfig(
                point_id="p1",
                point_groups=["fast"],
                address=PointAddress(type="hr"),
                sinks=["kafka"],  # type: ignore[call-arg]
            )

    def test_point_config_has_no_device_id(self) -> None:
        """点是设备无关的——``device_id`` 不再是 PointConfig 的字段。"""
        point = PointConfig(
            point_id="p001",
            point_groups=["fast"],
            address=PointAddress(ioa=1001),
            data_type="float32",
        )
        assert not hasattr(point, "device_id")
        with pytest.raises(ValidationError, match="extra_forbidden"):
            PointConfig(
                point_id="p001",
                point_groups=["fast"],
                device_id="wtg-001",  # type: ignore[call-arg]
                address=PointAddress(ioa=1001),
            )

    def test_negative_deadband_raises(self) -> None:
        with pytest.raises(ConfigError, match="deadband"):
            PointConfig(
                point_id="p1",
                point_groups=["fast"],
                address=PointAddress(type="hr"),
                deadband=-0.1,
            )

    def test_min_max_inverted_raises(self) -> None:
        with pytest.raises(ConfigError, match="min_value"):
            PointConfig(
                point_id="p1",
                point_groups=["fast"],
                address=PointAddress(type="hr"),
                min_value=10.0,
                max_value=5.0,
            )

    def test_non_numeric_type_skips_bounds_check(self) -> None:
        """min/max/deadband 仅对数值类型生效——bool/str 不校验。"""
        point = PointConfig(
            point_id="p1",
            point_groups=["signals"],
            address=PointAddress(type="single_point"),
            data_type="bool",
            min_value=10.0,
            max_value=5.0,
        )
        assert point.data_type == "bool"


class TestPointPatch:
    def test_point_groups_default_unset(self) -> None:
        """point_groups 未写时为 None 且不在 model_fields_set。"""
        patch = PointPatch(point_id="p001", max_value=2500.0)
        assert patch.point_groups is None
        assert patch.model_fields_set == {"point_id", "max_value"}

    def test_explicit_point_groups_replaces_whole(self) -> None:
        patch = PointPatch(point_id="p001", point_groups=["slow"])
        assert "point_groups" in patch.model_fields_set
        assert patch.point_groups == ["slow"]

    def test_group_and_sinks_removed(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            PointPatch(point_id="p001", group="fast")  # type: ignore[call-arg]
        with pytest.raises(ValidationError, match="extra_forbidden"):
            PointPatch(point_id="p001", sinks=["kafka"])  # type: ignore[call-arg]

    def test_empty_point_id_raises(self) -> None:
        with pytest.raises(ConfigError, match="non-empty"):
            PointPatch(point_id="")


class TestPointTablesConfig:
    """Raw 点表模型（``PointTableConfig`` / ``PointPatch``）的原始校验。"""

    def test_duplicate_point_id_in_table_raises(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate"):
            PointTableConfig(
                points=[
                    PointPatch(point_id="p001"),
                    PointPatch(point_id="p001"),
                ]
            )

    def test_duplicate_remove_points_raises(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate remove_points"):
            PointTableConfig(extends="base", remove_points=["p001", "p001"])

    def test_raw_defaults(self) -> None:
        cfg = PointTableConfig()
        assert cfg.extends is None
        assert cfg.remove_points == []
        assert cfg.points == []

    def test_same_point_id_across_tables_ok(self) -> None:
        """point_id 的命名空间是单份点表——不同表之间允许重复。"""
        cfg = PointTablesConfig(
            tables={
                "t1": PointTableConfig(points=[PointPatch(point_id="p001")]),
                "t2": PointTableConfig(points=[PointPatch(point_id="p001")]),
            }
        )
        assert len(cfg.tables) == 2


class TestResolvedPointTable:
    """继承展开后的完整点表（运行模型）校验。"""

    def test_invalid_data_type_raises(self) -> None:
        addr = PointAddress(type="holding_register")
        with pytest.raises(ConfigError, match="data_type"):
            ResolvedPointTable(
                points=[
                    PointConfig(
                        point_id="p1",
                        point_groups=["fast"],
                        address=addr,
                        data_type="imaginary",
                    )
                ]
            )

    def test_duplicate_point_id_raises(self) -> None:
        addr = PointAddress(type="holding_register")
        with pytest.raises(ConfigError, match="Duplicate"):
            ResolvedPointTable(
                points=[
                    PointConfig(
                        point_id="p001",
                        point_groups=["fast"],
                        address=addr,
                        data_type="float32",
                    ),
                    PointConfig(
                        point_id="p001",
                        point_groups=["slow"],
                        address=addr,
                        data_type="float32",
                    ),
                ]
            )

    def test_valid_config_builds(self) -> None:
        cfg = ResolvedPointTable(
            points=[
                PointConfig(
                    point_id="p001",
                    variable_name="rotor_speed",
                    point_groups=["fast", "telemetry"],
                    address=PointAddress(ioa=1001),
                    data_type="float32",
                ),
                PointConfig(
                    point_id="p002",
                    point_groups=["slow"],
                    address=PointAddress(type="measured_value", ioa=1002),
                    data_type="float32",
                ),
            ]
        )
        assert len(cfg.points) == 2
        assert cfg.points[0].variable_name == "rotor_speed"
        assert cfg.points[0].point_groups == ["fast", "telemetry"]
        assert cfg.points[1].point_groups == ["slow"]


class TestPointAddress:
    def test_extra_fields_allowed(self) -> None:
        """PointAddress allows arbitrary keys for protocol-specific addresses."""
        addr = PointAddress(type="holding_register", register=30001, slave=1)
        assert addr.type == "holding_register"
        assert addr.register == 30001  # type: ignore[attr-defined]
        assert addr.slave == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# CollectionTaskConfig / TasksConfig
# ---------------------------------------------------------------------------


def _task(**overrides: object) -> CollectionTaskConfig:
    data: dict[str, object] = {
        "task_id": "t1",
        "device": "d1",
        "point_group": "fast",
        "interval": 1.0,
        "targets": [TaskTarget(sink="s1")],
    }
    data.update(overrides)
    return CollectionTaskConfig(**data)  # type: ignore[arg-type]


class TestCollectionTaskConfig:
    def test_valid_device_task(self) -> None:
        task = _task()
        assert task.device == "d1"
        assert task.device_group is None
        assert task.enabled is True

    def test_valid_device_group_task(self) -> None:
        task = _task(device=None, device_group="turbine")
        assert task.device_group == "turbine"
        assert task.device is None

    def test_device_and_device_group_xor_both_set_raises(self) -> None:
        with pytest.raises(ConfigError, match="XOR"):
            _task(device="d1", device_group="turbine")

    def test_device_and_device_group_xor_neither_set_raises(self) -> None:
        with pytest.raises(ConfigError, match="XOR"):
            _task(device=None, device_group=None)

    def test_interval_must_be_positive(self) -> None:
        with pytest.raises(ConfigError, match="interval must be > 0"):
            _task(interval=0.0)
        with pytest.raises(ConfigError, match="interval must be > 0"):
            _task(interval=-1.0)

    def test_interval_optional_at_schema_level(self) -> None:
        """interval 在 schema 层可选——是否必填由加载期按协议能力跨文件校验。"""
        task = CollectionTaskConfig(
            task_id="t1",
            device="d1",
            point_group="fast",
            targets=[TaskTarget(sink="s1")],
        )
        assert task.interval is None

    def test_interval_must_be_positive_when_set(self) -> None:
        with pytest.raises(ConfigError, match="interval must be > 0"):
            CollectionTaskConfig(
                task_id="t1",
                device="d1",
                point_group="fast",
                interval=0.0,
                targets=[TaskTarget(sink="s1")],
            )

    def test_point_group_blank_raises(self) -> None:
        with pytest.raises(ConfigError, match="point_group must be non-empty"):
            _task(point_group="   ")

    def test_task_id_blank_raises(self) -> None:
        with pytest.raises(ConfigError, match="task_id must be non-empty"):
            _task(task_id="  ")

    def test_targets_empty_raises(self) -> None:
        with pytest.raises(ConfigError, match="targets must be non-empty"):
            _task(targets=[])

    def test_duplicate_target_sinks_raise(self) -> None:
        with pytest.raises(ConfigError, match="duplicate target sinks"):
            _task(targets=[TaskTarget(sink="s1"), TaskTarget(sink="s1")])

    def test_multiple_distinct_targets_ok(self) -> None:
        task = _task(targets=[TaskTarget(sink="s1"), TaskTarget(sink="s2")])
        assert [t.sink for t in task.targets] == ["s1", "s2"]


class TestTasksConfig:
    def test_duplicate_task_id_raises(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate task_id"):
            TasksConfig(tasks=[_task(task_id="t1"), _task(task_id="t1", interval=2.0)])

    def test_empty_tasks_ok(self) -> None:
        cfg = TasksConfig()
        assert cfg.tasks == []

    def test_unique_task_ids_ok(self) -> None:
        cfg = TasksConfig(tasks=[_task(task_id="t1"), _task(task_id="t2")])
        assert len(cfg.tasks) == 2
