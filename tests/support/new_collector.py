"""新 Collector（src/collector）测试共享装配辅助。

提供内存 CollectorConfig / CollectorDeviceSession / CollectorRuntime 构造与
Fake Protocol/NATS 发布替身，供 unit/component 层复用；不 import 任何
wind_hub_* 模块。Collector 不再持有 Sink，采样经 SamplePublisher 发往 NATS，
测试以 :class:`FakeNatsTransport` 记录已接受的 SampleMessage。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from collector.application.config import (
    CollectionTask,
    CollectorConfig,
    RuntimeParams,
)
from collector.application.identity import CollectorIdentity
from collector.application.nats_dispatch import SamplePublisher
from collector.application.runtime import CollectorRuntime
from collector.application.session import CollectorDeviceSession
from collector.domain.acquisition import AcquisitionEngine
from contracts.sample_message import SampleMessage
from core.application import (
    ProtocolSample,
    Quality,
)
from core.application.port.protocol import ListenHandle, SubscriptionHandle
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
)
from tests.support.new_commander import (
    FakeListenHandle,
    FakeProtocol,
    FakeRegistry,
    FakeSubscriptionHandle,
)
from tests.support.units import NONE_UNIT, minimal_unit_catalog

# ---------------------------------------------------------------------------
# Fake Protocol（采集增强版）
# ---------------------------------------------------------------------------


class CollectorFakeProtocol(FakeProtocol):
    """支持订阅录制/回放与 GI 记录的 Fake ProtocolPort。"""

    def __init__(self) -> None:
        super().__init__()
        self.subscriptions: list[tuple[tuple[str, ...], Any, float | None]] = []
        self.listeners: list[tuple[tuple[str, ...], Any, float | None]] = []
        self.read_error: Exception | None = None

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        if self.read_error is not None:
            raise self.read_error
        return await super().read_many(point_ids)

    def subscribe(
        self,
        point_ids: Sequence[str],
        callback: Any,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        # 同步创建订阅对象（不触发远程注册）；start() 由会话层显式执行。
        self.subscriptions.append((tuple(point_ids), callback, interval))
        return FakeSubscriptionHandle()

    async def listen(
        self,
        point_ids: Sequence[str],
        callback: Any,
        *,
        interval: float | None = None,
    ) -> ListenHandle:
        self.listeners.append((tuple(point_ids), callback, interval))
        return FakeListenHandle()

    async def emit(self, index: int, point_id: str, value: Any, quality: Quality = Quality.GOOD):
        """向第 index 个订阅回调推送一条样本（模拟协议原生订阅上送）。"""
        _, callback, _ = self.subscriptions[index]
        await self._push(callback, point_id, value, quality)

    async def emit_listener(
        self, index: int, point_id: str, value: Any, quality: Quality = Quality.GOOD
    ) -> None:
        """向第 index 个监听回调推送一条样本（模拟设备自发上送）。"""
        _, callback, _ = self.listeners[index]
        await self._push(callback, point_id, value, quality)

    @staticmethod
    async def _push(callback: Any, point_id: str, value: Any, quality: Quality) -> None:
        await callback(
            (
                ProtocolSample(
                    point_id=point_id,
                    value=value,
                    quality=quality,
                    timestamp=datetime.now(UTC),
                ),
            )
        )


class CollectorFakeRegistry(FakeRegistry):
    """产出 CollectorFakeProtocol 的注册表替身。"""

    # list 不变性：收窄基类 instances 元素类型需要显式忽略
    instances: list[CollectorFakeProtocol]  # type: ignore[assignment]

    def create(self, endpoint, point_table, protocol_options):
        instance = CollectorFakeProtocol()
        self.instances.append(instance)
        return instance


# ---------------------------------------------------------------------------
# Fake NATS 发布端口（PublisherPort）
# ---------------------------------------------------------------------------


class FakeNatsTransport:
    """记录式 NATS 发布替身：记录被接受的 SampleMessage。

    Attributes:
        published: publish 返回 True 的消息（已被“消息系统接受”）。
        rejected: publish 返回 False 或抛异常的消息。
        open_calls/close_calls: 生命周期调用计数。
        fail: 置 True 时 publish 返回 False（模拟 NATS 不可用）。
        raise_on_publish: 置异常时 publish 抛出（模拟意外故障）。
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        raise_on_publish: BaseException | None = None,
    ) -> None:
        self.published: list[SampleMessage] = []
        self.rejected: list[SampleMessage] = []
        self.open_calls = 0
        self.close_calls = 0
        self.fail = fail
        self.raise_on_publish = raise_on_publish

    async def open(self) -> None:
        self.open_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def publish(self, message: SampleMessage) -> bool:
        if self.raise_on_publish is not None:
            self.rejected.append(message)
            raise self.raise_on_publish
        if self.fail:
            self.rejected.append(message)
            return False
        self.published.append(message)
        return True


def make_sample_publisher(
    transport: FakeNatsTransport | None = None,
    *,
    wind_farm_id: str = "test-farm",
    delivery: str = "realtime",
    max_staleness_ms: int | None = 500,
) -> tuple[SamplePublisher, FakeNatsTransport]:
    """构造绑定 FakeNatsTransport 的应用层 SamplePublisher。"""
    transport = transport or FakeNatsTransport()
    identity = CollectorIdentity(
        collector_id="collector-test",
        boot_id="boot-test",
        config_hash="hash-test",
    )
    publisher = SamplePublisher(
        transport,
        wind_farm_id=wind_farm_id,
        identity=identity,
        delivery=delivery,  # type: ignore[arg-type]
        max_staleness_ms=max_staleness_ms if delivery == "realtime" else None,
    )
    return publisher, transport


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
    device_group_id: str | None = None,
    tasks: dict[str, CollectionTask] | None = None,
    messaging: MessagingSettings | None = None,
    ads_subscribe: bool = False,
    params: RuntimeParams | None = None,
) -> CollectorConfig:
    """构造单设备单点的内存 CollectorConfig（不经过 YAML）。

    默认含一个采集任务 ``t1``：device_group_id=dev1、group=g、interval=1.0。
    Task 不再声明输出去向（Sink 绑定在 sinks.yaml）。传 ``tasks={}`` 可关闭
    默认任务。
    """
    unit = NONE_UNIT
    point = Point(
        point_id="p1",
        business_point_id=BusinessPointId("p1"),
        source_unit=unit,
        access=PointAccess.READ_WRITE,
        scale=scale,
        offset=offset,
        ext={"register_type": "holding", "address": 100, "data_type": "float32"},
        point_groups=point_groups,
    )
    table = PointTable(
        point_table_id=PointTableId("tab"),
        protocol=Protocol(protocol),
        points={"p1": point},
    )
    group_ids = (DeviceGroupId(device_group_id or device_id),)
    device_models = {
        DeviceModelId("mod"): DeviceModel(
            device_model_id=DeviceModelId("mod"),
            device_type_id=DeviceTypeId("turbine"),
            point_table_id=PointTableId("tab"),
        )
    }
    devices = {
        DeviceId(device_id): Device(
            device_id=DeviceId(device_id),
            device_model_id=DeviceModelId("mod"),
            endpoint=ConnectionEndpoint(host="127.0.0.1", port=502),
            device_group_ids=group_ids,
            subscribe_enabled=ads_subscribe,
            protocol_options={"target_net_id": "127.0.0.1.1.1"} if protocol == "ads" else {},
        )
    }
    point_tables = {PointTableId("tab"): table}
    if tasks is None:
        tasks = {
            "t1": CollectionTask(
                task_id="t1",
                device_group_id=DeviceGroupId(device_group_id or device_id),
                point_group="g",
                interval=1.0,
            )
        }
    return CollectorConfig(
        units=minimal_unit_catalog(),
        messaging=messaging
        or MessagingSettings(
            servers=("nats://127.0.0.1:4222",),
            delivery="realtime",
            max_staleness_ms=500,
        ),
        device_models=device_models,
        devices=devices,
        point_tables=point_tables,
        runtime=params or RuntimeParams(connect_timeout=0.2, shutdown_timeout=0.5),
        tasks=tasks,
    )


def make_session(
    config: CollectorConfig,
    protocol: CollectorFakeProtocol | None = None,
    *,
    device_id: str = "dev1",
) -> CollectorDeviceSession:
    """按 Device 与 PointTable 构造采集会话（协议为 Fake）。"""
    device = config.devices[DeviceId(device_id)]
    return CollectorDeviceSession(
        device,
        config.point_table_for_device(DeviceId(device_id)),
        protocol or CollectorFakeProtocol(),
        subscribe_enabled=device.subscribe_enabled,
    )


def make_runtime(
    config: CollectorConfig,
    *,
    protocol: CollectorFakeProtocol | None = None,
    transport: FakeNatsTransport | None = None,
    engine: AcquisitionEngine | None = None,
) -> tuple[CollectorRuntime, CollectorFakeProtocol]:
    """装配内存 CollectorRuntime：单设备会话 + Fake NATS 发布 + 会话工厂。

    返回 (runtime, protocol)。调用方传入的 transport 可用于断言发布/拒绝
    记录；热重载 add/rebuild 路径与真实组合根同形（经 FakeRegistry 产新协议
    实例）。
    """
    proto = protocol or CollectorFakeProtocol()
    session = make_session(config, proto)
    registry = CollectorFakeRegistry()
    publisher, transport = make_sample_publisher(
        transport,
        delivery=config.messaging.delivery,
        max_staleness_ms=config.messaging.max_staleness_ms,
    )

    def session_factory(device: Device, point_table: PointTable) -> CollectorDeviceSession:
        return CollectorDeviceSession(
            device,
            point_table,
            registry.create(device.endpoint, point_table, device.protocol_options),
            subscribe_enabled=device.subscribe_enabled,
        )

    runtime = CollectorRuntime(
        devices={"dev1": session},
        publisher=publisher,
        engine=engine or AcquisitionEngine(),
        params=config.runtime,
        tasks=dict(config.tasks),
        session_factory=session_factory,
    )
    runtime.test_transport = transport  # type: ignore[attr-defined]
    return runtime, proto


__all__ = [
    "CollectorFakeProtocol",
    "CollectorFakeRegistry",
    "FakeNatsTransport",
    "make_collector_config",
    "make_runtime",
    "make_sample_publisher",
    "make_session",
]


# ---------------------------------------------------------------------------
# 配置目录写出辅助（config loader 测试）
# ---------------------------------------------------------------------------


def write_collector_config_tree(
    base: Path,
    *,
    protocol: str = "modbus",
    address: dict[str, Any] | None = None,
    devices: dict[str, Any] | None = None,
    points: dict[str, Any] | None = None,
    point_tables: dict[str, Any] | None = None,
    endpoint_defaults: dict[str, Any] | None = None,
    protocol_options: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    ads: dict[str, Any] | None = None,
    tasks: dict[str, Any] | None = None,
    sinks: dict[str, Any] | None = None,
    messaging: dict[str, Any] | None = None,
) -> Path:
    """写出 Collector 最小自包含配置树（含 tasks.yaml / sinks.yaml）。

    默认：单设备 dev1、单点 p1(group g)、单 Task ``t1``（不再含 outputs）、
    空 sinks 映射（Sink 归属 sink_service）、realtime NATS messaging。
    """
    from tests.support.new_commander import write_minimal_config_tree, write_yaml

    config_dir = write_minimal_config_tree(
        base,
        protocol=protocol,
        address=address,
        devices=devices,
        points=points,
        point_tables=point_tables,
        endpoint_defaults=endpoint_defaults,
        protocol_options=protocol_options,
        runtime=runtime,
        ads=ads,
    )
    write_yaml(config_dir, "sinks.yaml", {"sinks": sinks or {}})
    if tasks is None:
        tasks = {
            "t1": {
                "device_group_id": "dev1",
                "point_group": "g",
                "interval": 1.0,
            }
        }
    write_yaml(config_dir, "tasks.yaml", {"tasks": tasks})
    # Collector loader 强制要求 system.messaging；默认写最小 realtime 配置，
    # 调用方可经 messaging= 覆盖（传 {} 删除该段用于负向测试）。
    import yaml

    system_path = config_dir / "system.yaml"
    system_doc = yaml.safe_load(system_path.read_text(encoding="utf-8")) or {}
    if messaging is None:
        system_doc["messaging"] = {
            "backend": "nats",
            "servers": ["nats://127.0.0.1:4222"],
            "delivery": "realtime",
            "max_staleness_ms": 500,
        }
    elif messaging:
        system_doc["messaging"] = messaging
    else:
        system_doc.pop("messaging", None)
    write_yaml(config_dir, "system.yaml", system_doc)
    return config_dir
