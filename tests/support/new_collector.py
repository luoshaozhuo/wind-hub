"""新 Collector（src/collector）测试共享装配辅助。

提供内存 CollectorConfig / CollectorDeviceSession / CollectorRuntime 构造与
Fake Protocol/Sink，供 unit/component 层复用；不 import 任何 wind_hub_* 模块。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from collector.application.config import (
    CollectionTask,
    CollectorConfig,
    DeviceView,
    PointMeta,
    RuntimeParams,
)
from collector.application.runtime import CollectorRuntime
from collector.application.session import CollectorDeviceSession
from collector.domain.acquisition import AcquisitionEngine
from core.application import (
    ConnectionHealth,
    ProtocolSample,
    Quality,
)
from core.application.port import ConfigTopic
from core.application.port.protocol import SubscriptionHandle
from core.application.sink_config import SinksConfig
from core.domain import (
    BusinessPoint,
    BusinessPointId,
    ConnectionEndpoint,
    DataType,
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
)
from core.domain.config import (
    DeviceInstanceConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    EndpointConfig,
    PointConfig,
    PointTableConfig,
    PointTablesConfig,
    SystemConfig,
    TasksConfig,
    UnitDefinitionConfig,
    UnitsConfig,
)
from core.domain.unit import UNIT_CATALOG, UnitCode
from tests.support.new_commander import FakeProtocol, FakeRegistry, FakeSubscriptionHandle

# ---------------------------------------------------------------------------
# Fake Protocol（采集增强版）
# ---------------------------------------------------------------------------


class CollectorFakeProtocol(FakeProtocol):
    """支持订阅录制/回放与 GI 记录的 Fake ProtocolPort。"""

    def __init__(self) -> None:
        super().__init__()
        self.subscriptions: list[tuple[tuple[str, ...], Any, float | None]] = []
        self.interrogate_calls = 0
        self.read_error: Exception | None = None

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        if self.read_error is not None:
            raise self.read_error
        return await super().read_many(point_ids)

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: Any,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        self.subscriptions.append((tuple(point_ids), callback, interval))
        return FakeSubscriptionHandle()

    async def interrogate(self) -> None:
        self.interrogate_calls += 1

    async def emit(self, index: int, point_id: str, value: Any, quality: Quality = Quality.GOOD):
        """向第 index 个订阅回调推送一条样本（模拟协议上送）。"""
        _, callback, _ = self.subscriptions[index]
        await callback(
            ProtocolSample(
                point_id=point_id,
                value=value,
                quality=quality,
                timestamp=datetime.now(UTC),
            )
        )


class CollectorFakeRegistry(FakeRegistry):
    """产出 CollectorFakeProtocol 的注册表替身。"""

    instances: list[CollectorFakeProtocol]  # type: ignore[assignment]

    def create(self, endpoint, point_table, protocol_options_by_device):  # type: ignore[no-untyped-def]
        instance = CollectorFakeProtocol()
        self.instances.append(instance)
        return instance


# ---------------------------------------------------------------------------
# Fake Sink
# ---------------------------------------------------------------------------


class FakeSink:
    """记录式 SinkPort：open/close/flush/write 全部落账。"""

    def __init__(self, *, exclusive: bool = False) -> None:
        self.open_calls = 0
        self.close_calls = 0
        self.flush_calls = 0
        self.batches: list[list[Any]] = []
        self.fail_open = False
        self._exclusive = exclusive

    @property
    def exclusive_open(self) -> bool:
        return self._exclusive

    async def open(self) -> None:
        self.open_calls += 1
        if self.fail_open:
            raise ConnectionError("fake sink open failure")

    async def close(self) -> None:
        self.close_calls += 1

    async def write(self, batch: list[Any]) -> None:
        self.batches.append(list(batch))

    async def flush(self) -> None:
        self.flush_calls += 1

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=True)


# ---------------------------------------------------------------------------
# 内存 CollectorConfig
# ---------------------------------------------------------------------------


def make_collector_config(
    *,
    device_id: str = "dev1",
    protocol: str = "modbus",
    scale: float = 1.0,
    offset: float = 0.0,
    point_groups: tuple[str, ...] = ("g",),
    device_group: str | None = None,
    tasks: dict[str, CollectionTask] | None = None,
    sinks: dict[str, Any] | None = None,
    ads_subscribe: bool = False,
    params: RuntimeParams | None = None,
) -> CollectorConfig:
    """构造单设备单点的内存 CollectorConfig（不经过 YAML）。

    默认含一个采集任务 ``t1``：device=dev1、group=g、interval=1.0、
    targets=("s1",)。传 ``tasks={}`` 可关闭默认任务。
    """
    unit = UNIT_CATALOG[UnitCode.NONE]
    bp = BusinessPoint(
        business_point_id=BusinessPointId("p1"),
        data_type=DataType.FLOAT32,
        standard_unit=unit,
    )
    point = Point(
        point_id="p1",
        business_point_id=bp.business_point_id,
        source_unit=unit,
        access=PointAccess.READ_WRITE,
        scale=scale,
        offset=offset,
        ext={"register_type": "holding", "address": 100},
    )
    table = PointTable(
        point_table_id=PointTableId("tab"),
        protocol=Protocol(protocol),
        points={"p1": point},
    )
    group_ids = (DeviceGroupId(device_group),) if device_group else ()
    device_indexes = {
        "device_models": {
            DeviceModelId("mod"): DeviceModel(
                device_model_id=DeviceModelId("mod"),
                device_type_id=DeviceTypeId("turbine"),
                point_table_id=PointTableId("tab"),
            )
        },
        "devices": {
            DeviceId(device_id): Device(
                device_id=DeviceId(device_id),
                device_model_id=DeviceModelId("mod"),
                endpoint=ConnectionEndpoint(host="127.0.0.1", port=502),
                device_group_ids=group_ids,
            )
        },
        "point_tables": {PointTableId("tab"): table},
    }
    if tasks is None:
        tasks = {
            "t1": CollectionTask(
                task_id="t1",
                device=device_id,
                point_group="g",
                interval=1.0,
                targets=("s1",),
            )
        }
    config_vos = {
        ConfigTopic.SYSTEM: SystemConfig(),
        ConfigTopic.DEVICE_MODELS: DeviceModelsConfig(
            device_types={"turbine": None},
            device_models={
                "mod": DeviceModelConfig(
                    device_type="turbine",
                    manufacturer=None,
                    model=None,
                    protocol=protocol,
                    point_table="tab",
                    read_mode=None,
                    properties={},
                    connection_defaults={},
                )
            },
        ),
        ConfigTopic.DEVICES: DevicesConfig(
            devices={
                device_id: DeviceInstanceConfig(
                    device_id=device_id,
                    model="mod",
                    device_group=device_group,
                    endpoint=EndpointConfig(host="127.0.0.1", port=502, extensions={}),
                    enabled=True,
                )
            }
        ),
        ConfigTopic.POINTS: PointTablesConfig(
            tables={
                "tab": PointTableConfig(
                    protocol=protocol,
                    points={
                        "p1": PointConfig(
                            point_id="p1",
                            variable_name="P1",
                            point_groups=point_groups,
                            address={"register_type": "holding", "address": 100},
                            data_type="float32",
                            scale=scale,
                            offset=offset,
                            unit="none",
                            description=None,
                        )
                    },
                )
            }
        ),
        ConfigTopic.UNITS: UnitsConfig(
            units={"none": UnitDefinitionConfig(symbol="", name=None)}
        ),
        ConfigTopic.TASKS: TasksConfig(tasks=dict(tasks)),
        ConfigTopic.SINKS: SinksConfig(sinks=list((sinks or {}).values())),
    }
    return CollectorConfig(
        configs=config_vos,
        **device_indexes,
        runtime=params or RuntimeParams(connect_timeout=0.2, shutdown_timeout=0.5),
        tasks=tasks,
        sinks=sinks or {},
        point_meta={
            PointTableId("tab"): {"p1": PointMeta(variable_name="P1", point_groups=point_groups)}
        },
        ads_subscribe_devices=frozenset({DeviceId(device_id)}) if ads_subscribe else frozenset(),
    )


def with_config_vo(config: CollectorConfig, topic: ConfigTopic, value: Any) -> CollectorConfig:
    """返回替换单个主题配置 VO 基线后的新 CollectorConfig（不可变更新）。"""
    from dataclasses import replace

    return replace(config, configs={**config.configs, topic: value})


def make_session(
    config: CollectorConfig,
    protocol: CollectorFakeProtocol | None = None,
    *,
    device_id: str = "dev1",
) -> CollectorDeviceSession:
    """按 config 的 device_view 构造采集会话（协议为 Fake）。"""
    view = config.device_view(DeviceId(device_id))
    return CollectorDeviceSession(
        view.device,
        view.point_table,
        view.point_meta,
        protocol or CollectorFakeProtocol(),
        subscribe_enabled=view.subscribe_enabled,
        supports_scheduled_collection=view.supports_scheduled_collection,
    )


def make_runtime(
    config: CollectorConfig,
    *,
    protocol: CollectorFakeProtocol | None = None,
    sinks: dict[str, FakeSink] | None = None,
    engine: AcquisitionEngine | None = None,
    sink_factory: Any = None,
) -> tuple[CollectorRuntime, CollectorFakeProtocol]:
    """装配内存 CollectorRuntime：单设备会话 + Fake Sink + 会话工厂。

    返回 (runtime, protocol)；会话工厂经 FakeRegistry 产新协议实例，
    热重载 add/rebuild 路径与真实组合根同形。
    """
    proto = protocol or CollectorFakeProtocol()
    session = make_session(config, proto)
    views = {"dev1": config.device_view(DeviceId("dev1"))}
    registry = CollectorFakeRegistry()

    def session_factory(view: DeviceView) -> CollectorDeviceSession:
        return CollectorDeviceSession(
            view.device,
            view.point_table,
            view.point_meta,
            registry.create(view.device.endpoint, view.point_table, view.options),
            subscribe_enabled=view.subscribe_enabled,
            supports_scheduled_collection=view.supports_scheduled_collection,
        )

    runtime = CollectorRuntime(
        devices={"dev1": session},
        device_views=views,
        sinks=dict(sinks or {}),
        engine=engine or AcquisitionEngine(),
        params=config.runtime,
        tasks=dict(config.tasks),
        session_factory=session_factory,
        sink_factory=sink_factory,
    )
    return runtime, proto


__all__ = [
    "CollectorFakeProtocol",
    "CollectorFakeRegistry",
    "FakeSink",
    "make_collector_config",
    "make_runtime",
    "make_session",
    "with_config_vo",
]


# ---------------------------------------------------------------------------
# 配置目录写出辅助（config loader 测试）
# ---------------------------------------------------------------------------


def write_collector_config_tree(
    base: Path,
    *,
    protocol: str = "modbus",
    address: dict[str, Any] | None = None,
    devices: list[dict[str, Any]] | None = None,
    points: list[dict[str, Any]] | None = None,
    point_tables: dict[str, Any] | None = None,
    connection_defaults: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    ads: dict[str, Any] | None = None,
    read_mode: str | None = None,
    tasks: list[dict[str, Any]] | None = None,
    sinks: list[dict[str, Any]] | None = None,
) -> Path:
    """写出 Collector 最小自包含配置树（含 tasks.yaml / sinks.yaml）。

    默认：单设备 dev1、单点 p1(group g)、单 file Sink ``s1``（映射
    dev1.p1 → field value）、单 Task ``t1``（device=dev1, group=g,
    interval=1.0, targets=[s1]）。
    """
    from tests.support.new_commander import write_minimal_config_tree, write_yaml

    config_dir = write_minimal_config_tree(
        base,
        protocol=protocol,
        address=address,
        devices=devices,
        points=points,
        point_tables=point_tables,
        connection_defaults=connection_defaults,
        runtime=runtime,
        ads=ads,
        read_mode=read_mode,
    )
    if sinks is None:
        sinks = [
            {
                "name": "s1",
                "type": "file",
                "connection": {"path": str(base / "out.csv")},
                "points": [
                    {
                        "source": {"device_id": "dev1", "point_id": "p1"},
                        "address": {"field": "value"},
                    }
                ],
            }
        ]
    write_yaml(config_dir, "sinks.yaml", {"sinks": sinks})
    if tasks is None:
        tasks = [
            {
                "task_id": "t1",
                "device": "dev1",
                "point_group": "g",
                "interval": 1.0,
                "targets": [{"sink": "s1"}],
            }
        ]
    write_yaml(config_dir, "tasks.yaml", {"tasks": tasks})
    return config_dir
