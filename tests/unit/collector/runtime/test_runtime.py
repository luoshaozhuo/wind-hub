"""Runtime（``application/runtime``）的单元测试——Task / Task Instance 模型。

验证对象：:class:`Runtime`（组件生命周期、Task Instance 展开与启停、
acquisition handle 管理、热重载、Sink 背压/派发）。

覆盖点（对应重构简报 spec §30）：

- Task 展开：device 任务 → 1 实例；device_group 任务 → 组内全部启用设备
  各 1 实例（``{task_id}:{device_id}``）；disabled Task / 引用禁用设备的
  device Task 不展开；
- 启动语义：实例初始 STOPPED；``start_task_instance`` 幂等且对未知
  instance_id 抛 KeyError；
- 停止：``stop_task_instance`` 关闭采集句柄、幂等、stop 后不再 collect；
- polling 行为：fixed-rate（首次立即执行、interval 间隔）；单次 acquire
  异常只记日志继续；CancelledError 传播；
- 停机：``stop()`` 关闭全部采集句柄、无孤儿协程；
- 热重载 ``reconfigure``：Task 增删 / device_group 成员变化 / interval
  变化重建句柄、targets 快照替换（句柄不重启）/ 点表重注入 / 连接不重建；
- Sink 派发与背压：targets fan-out、未知 sink 跳过、drop_old / drop_new。

引擎侧循环测试使用内存 ``_FakeEngine``（只实现 Runtime 依赖的装配缝与
``collect``），Sink fan-out 使用真实 :class:`AcquisitionEngine` 验证。
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub_collector.application.command_dispatcher import CommandDispatcher
from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.runtime import Runtime
from wind_hub_collector.application.runtime.device import Device
from wind_hub_collector.application.runtime.task_instance import (
    CollectionTaskInstance,
    TaskInstanceState,
    task_instance_id,
)
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config.schema import (
    CollectionTaskConfig,
    Config,
    DeviceConfig,
    DevicesConfig,
    PointAddress,
    PointConfig,
    ResolvedPointTable,
    ResolvedPointTables,
    RuntimeConfig,
    SinkConfig,
    SystemConfig,
    TasksConfig,
    TaskTarget,
    UnitConfig,
    UnitsConfig,
)
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue
from wind_hub_core.model.reload import ConfigDiff, DeviceDiff, TaskDiff
from wind_hub_core.protocol.port import AcquisitionMode, ProtocolPort

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_device_config(
    device_id: str,
    device_group: str | None = None,
    enabled: bool = True,
    point_table: str = "t1",
    protocol: str = "modbus",
) -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol=protocol,
        endpoint=Endpoint(host="10.0.0.1", port=502),
        point_table=point_table,
        device_group=device_group,
        enabled=enabled,
    )


def _make_task(
    task_id: str,
    device: str | None = None,
    device_group: str | None = None,
    point_group: str = "g1",
    interval: float = 0.02,
    sinks: tuple[str, ...] = ("s1",),
    enabled: bool = True,
) -> CollectionTaskConfig:
    return CollectionTaskConfig(
        task_id=task_id,
        device=device,
        device_group=device_group,
        point_group=point_group,
        interval=interval,
        targets=[TaskTarget(sink=s) for s in sinks],
        enabled=enabled,
    )


def _make_point(point_id: str, groups: tuple[str, ...] = ("g1",)) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        point_groups=list(groups),
        address=PointAddress(type="holding_register"),
    )


def _mock_protocol() -> ProtocolPort:
    proto = MagicMock(spec=ProtocolPort)
    proto.set_points_mapping = MagicMock()
    proto.connect = AsyncMock()
    proto.close = AsyncMock()
    proto.read = AsyncMock(return_value=[])
    proto.write = AsyncMock()
    proto.health = MagicMock(return_value=HealthStatus(healthy=True))
    # 默认主动轮询型设备（Modbus 语义）——经 Device.start_acquisition
    # 走 PollingAcquisitionHandle。
    proto.acquisition_mode = AcquisitionMode.POLL
    return proto


def _mock_sink() -> SinkPort:
    sink = MagicMock(spec=SinkPort)
    sink.open = AsyncMock()
    sink.close = AsyncMock()
    sink.write = AsyncMock()
    sink.flush = AsyncMock()
    sink.health = MagicMock(return_value=HealthStatus(healthy=True))
    return sink


def _value(device_id: str = "d1", point_id: str = "p1") -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=1.0)


def _runtime_config(backpressure: str = "drop_old", queue_maxsize: int = 10) -> RuntimeConfig:
    return RuntimeConfig(
        queue_maxsize=queue_maxsize,
        backpressure_policy=backpressure,
        shutdown_timeout=0.5,
        connect_timeout=0.2,
        read_timeout=0.2,
    )


class _FakeEngine:
    """``AcquisitionEngine`` 的内存替身——只实现 Runtime 依赖的接口面。

    记录每次 ``collect`` 调用的完整参数；``fail_next`` 让下一次 collect
    抛异常（验证 polling 循环的异常韧性）；``collect_gate`` 可阻塞 collect
    （验证 CancelledError 传播）。不模拟引擎内部读/派发逻辑——那是
    ``tests/unit/acquisition`` 的职责。
    """

    def __init__(self) -> None:
        self.collect_calls: list[tuple[str, str, list[str], str]] = []
        self.process_calls: list[tuple[list[PointValue], list[str]]] = []
        self.fail_next = 0
        self.collect_gate: asyncio.Event | None = None
        self.sink_dispatch: object | None = None

    def attach_sink_dispatch(self, dispatch: object) -> None:
        self.sink_dispatch = dispatch

    def attach_device_state(self, device_state: object) -> None:
        pass

    def attach_acquisition_state(self, acquisition_state: object) -> None:
        pass

    @property
    def points_collected(self) -> int:
        return 0

    async def collect(
        self,
        device: Device,
        point_group: str,
        targets: list[str],
        execution_id: str,
    ) -> None:
        self.collect_calls.append((device.device_id, point_group, list(targets), execution_id))
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("collect boom")
        if self.collect_gate is not None:
            await self.collect_gate.wait()

    async def process(self, batch: list[PointValue], targets: list[str]) -> None:
        self.process_calls.append((batch, list(targets)))


def _build_devices(
    configs: list[DeviceConfig],
    protos: dict[str, ProtocolPort],
    points: dict[str, list[PointConfig]],
) -> dict[str, Device]:
    """按装配语义构建运行时 Device：构造期注入点映射并聚合配置/点表/协议。"""
    devices: dict[str, Device] = {}
    for cfg in configs:
        proto = protos[cfg.device_id]
        device_points = points.get(cfg.device_id, [])
        devices[cfg.device_id] = Device(cfg, device_points, proto)
    return devices


def _build_runtime(
    *,
    devices: list[DeviceConfig],
    tasks: list[CollectionTaskConfig],
    sink_names: tuple[str, ...] = ("s1",),
    points: dict[str, list[PointConfig]] | None = None,
    engine: _FakeEngine | None = None,
    backpressure: str = "drop_old",
    queue_maxsize: int = 10,
    protocol_factory=None,
    sink_factory=None,
    metrics_hook=None,
) -> tuple[Runtime, dict[str, ProtocolPort], dict[str, SinkPort], _FakeEngine]:
    protos = {d.device_id: _mock_protocol() for d in devices}
    sinks = {name: _mock_sink() for name in sink_names}
    eng = engine if engine is not None else _FakeEngine()
    device_map = _build_devices(devices, protos, points if points is not None else {})
    rt = Runtime(
        devices=device_map,
        sinks=sinks,
        engine=eng,  # type: ignore[arg-type]  # 鸭子类型替身，仅实现 Runtime 依赖面
        dispatcher=CommandDispatcher(device_map),
        config=_runtime_config(backpressure, queue_maxsize),
        tasks={t.task_id: t for t in tasks},
        protocol_factory=protocol_factory,
        sink_factory=sink_factory,
        metrics_hook=metrics_hook,
    )
    return rt, protos, sinks, eng


def _full_config(
    *,
    devices: list[DeviceConfig],
    tasks: list[CollectionTaskConfig],
    tables: dict[str, ResolvedPointTable] | None = None,
    sink_names: tuple[str, ...] = ("s1", "s2"),
) -> Config:
    if tables is None:
        tables = {"t1": ResolvedPointTable(protocol="modbus", points=[_make_point("p1")])}
    return Config(
        system=SystemConfig(
            runtime=_runtime_config(),
            sinks=[SinkConfig(name=n, type="file") for n in sink_names],
        ),
        units=UnitsConfig(units={"none": UnitConfig(symbol="")}),
        devices=DevicesConfig(devices=list(devices)),
        point_tables=ResolvedPointTables(tables=tables),
        tasks=TasksConfig(tasks=list(tasks)),
    )


async def _wait_for(cond, timeout: float = 2.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError(f"timeout waiting for {what}")
        await asyncio.sleep(0.005)


def _instance_coroutine_tasks() -> list[asyncio.Task]:
    """当前事件循环中仍在跑的 polling 协程（孤儿检测用）。"""
    result = []
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        if coro is not None and "PollingAcquisitionHandle._run" in coro.__qualname__:
            result.append(task)
    return result


# ---------------------------------------------------------------------------
# 连接状态与显式强制重连
# ---------------------------------------------------------------------------


class TestEnsureConnected:
    async def test_force_checks_driver_health_before_fast_path(self) -> None:
        """Runtime 显示 connected 但 Driver unhealthy 时，force=True 必须重连。"""
        rt, protos, _, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[],
        )
        await rt.start()
        try:
            proto = protos["d1"]
            assert rt.device_state("d1") is not None
            assert rt.device_state("d1").connected is True

            proto.connect.reset_mock()
            proto.health.return_value = HealthStatus(
                healthy=False,
                message="driver disconnected",
            )

            assert await rt.ensure_connected("d1", force=True) is True

            proto.health.assert_called_once()
            proto.connect.assert_awaited_once()
            assert rt.device_state("d1").connected is True
        finally:
            await rt.stop()

    async def test_force_healthy_driver_keeps_zero_io_fast_path(self) -> None:
        """Runtime 与 Driver 都健康时，force=True 不重复 connect。"""
        rt, protos, _, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[],
        )
        await rt.start()
        try:
            proto = protos["d1"]
            proto.connect.reset_mock()
            proto.health.return_value = HealthStatus(healthy=True)

            assert await rt.ensure_connected("d1", force=True) is True

            proto.health.assert_called_once()
            proto.connect.assert_not_awaited()
        finally:
            await rt.stop()

    async def test_force_unhealthy_driver_connect_failure_marks_disconnected(self) -> None:
        """强制重连失败必须把 Runtime 状态同步为 disconnected。"""
        rt, protos, _, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[],
        )
        await rt.start()
        try:
            proto = protos["d1"]
            proto.connect.reset_mock()
            proto.health.return_value = HealthStatus(
                healthy=False,
                message="driver disconnected",
            )
            proto.connect.side_effect = OSError("connection refused")

            assert await rt.ensure_connected("d1", force=True) is False

            proto.connect.assert_awaited_once()
            state = rt.device_state("d1")
            assert state is not None
            assert state.connected is False
            assert "connection refused" in (state.last_error or "")
        finally:
            await rt.stop()


# ---------------------------------------------------------------------------
# Task 展开
# ---------------------------------------------------------------------------


class TestTaskExpansion:
    async def test_device_task_expands_to_single_instance(self) -> None:
        rt, _, _, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=1.0, sinks=("s1", "s2"))],
            sink_names=("s1", "s2"),
        )
        await rt.start()
        try:
            instances = rt.task_instances()
            assert list(instances) == ["t1:d1"]
            inst = instances["t1:d1"]
            assert isinstance(inst, CollectionTaskInstance)
            assert inst.task_id == "t1"
            assert inst.device_id == "d1"
            assert inst.point_group == "g1"
            assert inst.interval == 1.0
            assert inst.targets == ["s1", "s2"]
        finally:
            await rt.stop()

    async def test_device_group_task_expands_to_enabled_group_members(self) -> None:
        rt, _, _, _ = _build_runtime(
            devices=[
                _make_device_config("d1", device_group="turbine"),
                _make_device_config("d2", device_group="turbine"),
                _make_device_config("d3", device_group="turbine", enabled=False),
                _make_device_config("d4", device_group="pcs"),
            ],
            tasks=[_make_task("tg", device_group="turbine")],
        )
        await rt.start()
        try:
            assert set(rt.task_instances()) == {"tg:d1", "tg:d2"}
        finally:
            await rt.stop()

    async def test_disabled_task_expands_to_nothing(self) -> None:
        rt, _, _, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", enabled=False)],
        )
        await rt.start()
        try:
            assert rt.task_instances() == {}
        finally:
            await rt.stop()

    async def test_device_task_on_disabled_device_expands_to_nothing(self) -> None:
        rt, _, _, _ = _build_runtime(
            devices=[_make_device_config("d1", enabled=False)],
            tasks=[_make_task("t1", device="d1")],
        )
        await rt.start()
        try:
            assert rt.task_instances() == {}
        finally:
            await rt.stop()

    async def test_device_task_on_unknown_device_expands_to_nothing(self) -> None:
        rt, _, _, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="ghost")],
        )
        await rt.start()
        try:
            assert rt.task_instances() == {}
        finally:
            await rt.stop()

    async def test_instances_registered_stopped(self) -> None:
        rt, protos, _, eng = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1")],
        )
        await rt.start()
        try:
            assert rt.instance_states() == {"t1:d1": TaskInstanceState.STOPPED}
            # STOPPED 实例不执行任何采集
            await asyncio.sleep(0.05)
            assert eng.collect_calls == []
            # 采集执行状态簿按实例预注册
            acq = rt.acquisition_states()
            assert acq["t1:d1"].task_id == "t1"
            assert acq["t1:d1"].running is False
            assert protos["d1"].connect.await_count == 1
        finally:
            await rt.stop()

    def test_task_instance_id_convention(self) -> None:
        assert task_instance_id("t1", "d1") == "t1:d1"


# ---------------------------------------------------------------------------
# 启动 / 停止语义
# ---------------------------------------------------------------------------


class TestStartStopInstance:
    async def test_start_instance_begins_polling(self) -> None:
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=0.02)],
        )
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
            await _wait_for(lambda: len(eng.collect_calls) >= 1, what="first collect")
            device_id, group, targets, execution_id = eng.collect_calls[0]
            assert (device_id, group, targets, execution_id) == ("d1", "g1", ["s1"], "t1:d1")
        finally:
            await rt.stop()

    async def test_start_instance_idempotent_no_second_handle(self) -> None:
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=0.02)],
        )
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            first = rt._acquisition_handles["t1:d1"]  # noqa: SLF001
            await rt.start_task_instance("t1:d1")
            await rt.start_task_instance("t1:d1")
            assert rt._acquisition_handles["t1:d1"] is first  # noqa: SLF001
            assert len(rt._acquisition_handles) == 1  # noqa: SLF001
            assert len(_instance_coroutine_tasks()) == 1
            await _wait_for(lambda: len(eng.collect_calls) >= 2, what="polling continues")
        finally:
            await rt.stop()

    async def test_start_unknown_instance_raises_key_error(self) -> None:
        rt, _, _, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1")],
        )
        await rt.start()
        try:
            with pytest.raises(KeyError):
                await rt.start_task_instance("nope:d1")
        finally:
            await rt.stop()

    async def test_stop_instance_closes_handle(self) -> None:
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=0.02)],
        )
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await _wait_for(lambda: len(eng.collect_calls) >= 1, what="first collect")
            await rt.stop_task_instance("t1:d1")
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.STOPPED
            assert "t1:d1" not in rt._acquisition_handles  # noqa: SLF001
            assert _instance_coroutine_tasks() == []
            # stop 后不再 collect
            count = len(eng.collect_calls)
            await asyncio.sleep(0.08)
            assert len(eng.collect_calls) == count
        finally:
            await rt.stop()

    async def test_stop_instance_idempotent(self) -> None:
        rt, _, _, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=0.02)],
        )
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await rt.stop_task_instance("t1:d1")
            await rt.stop_task_instance("t1:d1")  # 已 STOPPED——幂等不抛
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.STOPPED
        finally:
            await rt.stop()

    async def test_stop_unknown_instance_raises_key_error(self) -> None:
        rt, _, _, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1")],
        )
        await rt.start()
        try:
            with pytest.raises(KeyError):
                await rt.stop_task_instance("nope:d1")
        finally:
            await rt.stop()

    async def test_stop_instance_does_not_affect_others(self) -> None:
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1"), _make_device_config("d2")],
            tasks=[
                _make_task("t1", device="d1", interval=0.02),
                _make_task("t2", device="d2", interval=0.02),
            ],
        )
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await rt.start_task_instance("t2:d2")
            await rt.stop_task_instance("t1:d1")
            assert rt.instance_states()["t2:d2"] is TaskInstanceState.RUNNING
            await _wait_for(
                lambda: any(c[0] == "d2" for c in eng.collect_calls),
                what="d2 keeps collecting",
            )
        finally:
            await rt.stop()


# ---------------------------------------------------------------------------
# polling 循环行为
# ---------------------------------------------------------------------------


class TestPollingLoop:
    async def test_interval_respected_between_cycles(self) -> None:
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=10.0)],
        )
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await _wait_for(lambda: len(eng.collect_calls) == 1, what="first collect")
            # interval 远大于观测窗口——窗口内不应有第二轮
            await asyncio.sleep(0.1)
            assert len(eng.collect_calls) == 1
        finally:
            await rt.stop()

    async def test_repeated_cycles_with_short_interval(self) -> None:
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=0.02)],
        )
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await _wait_for(lambda: len(eng.collect_calls) >= 3, what="multiple cycles")
        finally:
            await rt.stop()

    async def test_acquire_exception_logged_and_loop_continues(self, caplog) -> None:
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=0.02)],
        )
        await rt.start()
        try:
            with caplog.at_level(
                logging.WARNING, logger="wind_hub_collector.application.runtime.device"
            ):
                eng.fail_next = 1
                await rt.start_task_instance("t1:d1")
                await _wait_for(lambda: len(eng.collect_calls) >= 3, what="loop survives failure")
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
            assert rt.running is True
            assert any("Polling acquire failed" in record.getMessage() for record in caplog.records)
        finally:
            await rt.stop()

    async def test_cancelled_error_propagates_during_collect(self) -> None:
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=0.02)],
        )
        eng.collect_gate = asyncio.Event()  # 永不 set——collect 内阻塞
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await _wait_for(lambda: len(eng.collect_calls) == 1, what="collect entered")
            # 取消发生在 collect 内部等待期间——CancelledError 必须传播，
            # 循环不被吞掉、实例正确归位 STOPPED。
            await rt.stop_task_instance("t1:d1")
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.STOPPED
            assert _instance_coroutine_tasks() == []
        finally:
            await rt.stop()

    async def test_running_instance_reads_updated_snapshot_next_cycle(self) -> None:
        """采集回调每轮现取实例——targets 快照替换后下一轮生效（不重启句柄）。"""
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=0.02)],
        )
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await _wait_for(lambda: len(eng.collect_calls) >= 1, what="first collect")
            handle = rt._acquisition_handles["t1:d1"]  # noqa: SLF001
            # 就地替换实例快照（reconfigure 的内部机制）——仅 targets 变化
            rt._task_instances["t1:d1"] = CollectionTaskInstance(  # noqa: SLF001
                instance_id="t1:d1",
                task_id="t1",
                device_id="d1",
                point_group="g1",
                interval=0.02,
                targets=["s9"],
            )
            await _wait_for(
                lambda: any(c[2] == ["s9"] for c in eng.collect_calls),
                what="new snapshot picked up",
            )
            assert rt._acquisition_handles["t1:d1"] is handle  # noqa: SLF001
        finally:
            await rt.stop()


# ---------------------------------------------------------------------------
# 停机
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_stop_closes_all_handles_no_orphans(self) -> None:
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1"), _make_device_config("d2")],
            tasks=[
                _make_task("t1", device="d1", interval=0.02),
                _make_task("t2", device="d2", interval=0.02),
            ],
        )
        await rt.start()
        await rt.start_task_instance("t1:d1")
        await rt.start_task_instance("t2:d2")
        await _wait_for(lambda: len(eng.collect_calls) >= 2, what="both polling")

        await rt.stop()

        assert rt._acquisition_handles == {}  # noqa: SLF001
        assert _instance_coroutine_tasks() == []
        assert set(rt.instance_states().values()) == {TaskInstanceState.STOPPED}
        assert rt.running is False
        count = len(eng.collect_calls)
        await asyncio.sleep(0.06)
        assert len(eng.collect_calls) == count

    async def test_stop_idempotent(self) -> None:
        rt, _, _, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=0.02)],
        )
        await rt.start()
        await rt.start_task_instance("t1:d1")
        await rt.stop()
        await rt.stop()  # 幂等
        assert rt.running is False
        assert _instance_coroutine_tasks() == []

    async def test_restart_requires_explicit_instance_start(self) -> None:
        rt, _, _, eng = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1", interval=0.02)],
        )
        await rt.start()
        await rt.start_task_instance("t1:d1")
        await rt.stop()

        await rt.start()
        try:
            # 实例保留、统一 STOPPED——重启后不自动恢复采集
            assert rt.instance_states() == {"t1:d1": TaskInstanceState.STOPPED}
            count = len(eng.collect_calls)
            await asyncio.sleep(0.06)
            assert len(eng.collect_calls) == count
        finally:
            await rt.stop()


# ---------------------------------------------------------------------------
# 热重载 reconfigure
# ---------------------------------------------------------------------------


class TestReconfigure:
    async def test_add_task_registers_stopped_instance(self) -> None:
        devices = [_make_device_config("d1")]
        rt, protos, _, eng = _build_runtime(devices=devices, tasks=[])
        await rt.start()
        try:
            assert rt.task_instances() == {}
            new_cfg = _full_config(devices=devices, tasks=[_make_task("t1", device="d1")])
            errors = await rt.reconfigure(new_cfg, ConfigDiff(tasks=TaskDiff(added=["t1"])))
            assert errors == []
            assert set(rt.task_instances()) == {"t1:d1"}
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.STOPPED
            await asyncio.sleep(0.05)
            assert eng.collect_calls == []
            # Task 增删不触碰设备连接
            assert protos["d1"].connect.await_count == 1
        finally:
            await rt.stop()

    async def test_remove_task_unregisters_running_instance(self) -> None:
        devices = [_make_device_config("d1")]
        task = _make_task("t1", device="d1", interval=0.02)
        rt, _, _, eng = _build_runtime(devices=devices, tasks=[task])
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await _wait_for(lambda: len(eng.collect_calls) >= 1, what="polling")
            new_cfg = _full_config(devices=devices, tasks=[])
            errors = await rt.reconfigure(new_cfg, ConfigDiff(tasks=TaskDiff(removed=["t1"])))
            assert errors == []
            assert rt.task_instances() == {}
            assert rt.instance_states() == {}
            assert rt.acquisition_states() == {}
            assert _instance_coroutine_tasks() == []
            count = len(eng.collect_calls)
            await asyncio.sleep(0.06)
            assert len(eng.collect_calls) == count
        finally:
            await rt.stop()

    async def test_device_group_membership_change_adds_and_removes_instances(self) -> None:
        """d1 移出分组（轻量更新，不重建连接）+ d2 热增入组 → 实例增删。"""
        d1 = _make_device_config("d1", device_group="turbine")
        task = _make_task("tg", device_group="turbine")
        factory = MagicMock(side_effect=lambda cfg: _mock_protocol())
        rt, protos, _, _ = _build_runtime(devices=[d1], tasks=[task], protocol_factory=factory)
        await rt.start()
        try:
            assert set(rt.task_instances()) == {"tg:d1"}
            d1_new = _make_device_config("d1", device_group="pcs")
            d2_new = _make_device_config("d2", device_group="turbine")
            new_cfg = _full_config(devices=[d1_new, d2_new], tasks=[task])
            diff = ConfigDiff(devices=DeviceDiff(added=["d2"], updated=["d1"]))
            errors = await rt.reconfigure(new_cfg, diff)
            assert errors == []
            assert set(rt.task_instances()) == {"tg:d2"}
            assert rt.instance_states()["tg:d2"] is TaskInstanceState.STOPPED
            # d1 只改了 device_group——轻量路径，不重建连接
            assert protos["d1"].connect.await_count == 1
            assert rt.devices["d1"].protocol is protos["d1"]
            # d2 走热增路径：新协议实例由工厂创建并连接
            assert factory.call_count == 1
            assert rt.devices["d2"].protocol.connect.await_count == 1
            assert rt.running is True
        finally:
            await rt.stop()

    async def test_device_disable_removes_instances_without_runtime_restart(self) -> None:
        d1 = _make_device_config("d1", device_group="turbine")
        task = _make_task("tg", device_group="turbine")
        factory = MagicMock(side_effect=lambda cfg: _mock_protocol())
        rt, protos, _, _ = _build_runtime(devices=[d1], tasks=[task], protocol_factory=factory)
        await rt.start()
        try:
            await rt.start_task_instance("tg:d1")
            assert rt.instance_states()["tg:d1"] is TaskInstanceState.RUNNING
            old_proto = protos["d1"]
            d1_disabled = _make_device_config("d1", device_group="turbine", enabled=False)
            new_cfg = _full_config(devices=[d1_disabled], tasks=[task])
            errors = await rt.reconfigure(new_cfg, ConfigDiff(devices=DeviceDiff(updated=["d1"])))
            assert errors == []
            assert rt.task_instances() == {}
            assert _instance_coroutine_tasks() == []
            # enabled 翻转走重建路径：旧连接关闭、新驱动由工厂创建并接入。
            assert old_proto.close.await_count == 1
            assert factory.call_count == 1
            new_proto = rt.devices["d1"].protocol
            assert new_proto is not old_proto
            assert new_proto.connect.await_count == 1
            assert rt.running is True
        finally:
            await rt.stop()

    async def test_task_targets_update_keeps_handle(self) -> None:
        """仅 targets 变化：快照替换，运行中的采集句柄不重建。"""
        devices = [_make_device_config("d1")]
        task = _make_task("t1", device="d1", interval=0.02, sinks=("s1",))
        rt, protos, _, eng = _build_runtime(devices=devices, tasks=[task], sink_names=("s1", "s2"))
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await _wait_for(lambda: len(eng.collect_calls) >= 1, what="polling")
            handle = rt._acquisition_handles["t1:d1"]  # noqa: SLF001

            updated = _make_task("t1", device="d1", interval=0.02, sinks=("s2",))
            new_cfg = _full_config(devices=devices, tasks=[updated])
            errors = await rt.reconfigure(new_cfg, ConfigDiff(tasks=TaskDiff(updated=["t1"])))
            assert errors == []
            inst = rt.task_instances()["t1:d1"]
            assert inst.targets == ["s2"]
            assert rt._acquisition_handles["t1:d1"] is handle  # noqa: SLF001
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
            await _wait_for(
                lambda: any(c[2] == ["s2"] for c in eng.collect_calls),
                what="collect with new targets",
            )
            # target 变化不重建设备连接
            assert protos["d1"].connect.await_count == 1
        finally:
            await rt.stop()

    async def test_task_interval_update_replaces_handle(self) -> None:
        """interval 变化：运行中实例的采集句柄被替换（poll 重新对齐节拍），
        实例保持 RUNNING。"""
        devices = [_make_device_config("d1")]
        task = _make_task("t1", device="d1", interval=10.0, sinks=("s1",))
        rt, protos, _, eng = _build_runtime(devices=devices, tasks=[task], sink_names=("s1", "s2"))
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await _wait_for(lambda: len(eng.collect_calls) >= 1, what="first collect")
            old_handle = rt._acquisition_handles["t1:d1"]  # noqa: SLF001
            # 长 interval——窗口内只有一轮
            await asyncio.sleep(0.05)
            assert len(eng.collect_calls) == 1

            updated = _make_task("t1", device="d1", interval=0.02, sinks=("s1",))
            new_cfg = _full_config(devices=devices, tasks=[updated])
            errors = await rt.reconfigure(new_cfg, ConfigDiff(tasks=TaskDiff(updated=["t1"])))
            assert errors == []
            inst = rt.task_instances()["t1:d1"]
            assert inst.interval == 0.02
            new_handle = rt._acquisition_handles["t1:d1"]  # noqa: SLF001
            assert new_handle is not old_handle
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
            # 旧句柄已关闭——无孤儿协程
            assert len(_instance_coroutine_tasks()) == 1
            # 新节拍生效：短 interval 下连续多轮
            await _wait_for(lambda: len(eng.collect_calls) >= 3, what="new cadence")
            assert protos["d1"].connect.await_count == 1
        finally:
            await rt.stop()

    async def test_point_table_change_reinjects_mapping_without_reconnect(self) -> None:
        devices = [_make_device_config("d1")]
        task = _make_task("t1", device="d1")
        points = {"d1": [_make_point("p1")]}
        rt, protos, _, _ = _build_runtime(devices=devices, tasks=[task], points=points)
        await rt.start()
        try:
            assert protos["d1"].set_points_mapping.call_count == 1
            new_tables = {
                "t1": ResolvedPointTable(
                    protocol="modbus", points=[_make_point("p1"), _make_point("p2")]
                )
            }
            new_cfg = _full_config(devices=devices, tasks=[task], tables=new_tables)
            diff = ConfigDiff(points_changed=True, point_tables_changed=["t1"])
            errors = await rt.reconfigure(new_cfg, diff)
            assert errors == []
            # 映射重注入（2 个点），连接不重建
            assert protos["d1"].set_points_mapping.call_count == 2
            injected = protos["d1"].set_points_mapping.call_args[0][0]
            assert [p.point_id for p in injected] == ["p1", "p2"]
            assert protos["d1"].connect.await_count == 1
            assert [p.point_id for p in rt.devices["d1"].points] == ["p1", "p2"]
        finally:
            await rt.stop()


    async def test_lightweight_device_update_and_point_table_change_reinjects_mapping(self) -> None:
        """device_group 轻量更新与点表内容变化同时发生时不得漏掉新 mapping。"""
        d1 = _make_device_config("d1", device_group="old")
        task = _make_task("t1", device="d1")
        rt, protos, _, _ = _build_runtime(
            devices=[d1],
            tasks=[task],
            points={"d1": [_make_point("p1")]},
        )
        await rt.start()
        try:
            d1_new = _make_device_config("d1", device_group="new")
            new_tables = {
                "t1": ResolvedPointTable(
                    protocol="modbus",
                    points=[_make_point("p1"), _make_point("p2")],
                )
            }
            new_cfg = _full_config(devices=[d1_new], tasks=[task], tables=new_tables)
            diff = ConfigDiff(
                devices=DeviceDiff(updated=["d1"]),
                points_changed=True,
                point_tables_changed=["t1"],
            )

            errors = await rt.reconfigure(new_cfg, diff)

            assert errors == []
            assert rt.devices["d1"].config.device_group == "new"
            assert [p.point_id for p in rt.devices["d1"].points] == ["p1", "p2"]
            assert protos["d1"].set_points_mapping.call_count == 2
            assert protos["d1"].connect.await_count == 1
        finally:
            await rt.stop()


    async def test_point_table_change_restarts_running_subscription_handle(self) -> None:
        """订阅型设备点表变化时，RUNNING instance 必须注销旧订阅并重新注册。"""
        devices = [_make_device_config("d1", protocol="ads")]
        task = _make_task("t1", device="d1", interval=0.02)
        points = {"d1": [_make_point("p1")]}
        rt, protos, _, _ = _build_runtime(devices=devices, tasks=[task], points=points)
        proto = protos["d1"]
        proto.acquisition_mode = AcquisitionMode.SUBSCRIBE

        first_subscription = MagicMock()
        first_subscription.close = AsyncMock()
        second_subscription = MagicMock()
        second_subscription.close = AsyncMock()
        proto.subscribe = AsyncMock(side_effect=[first_subscription, second_subscription])

        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            first_handle = rt._acquisition_handles["t1:d1"]  # noqa: SLF001
            assert proto.subscribe.await_count == 1

            new_tables = {
                "t1": ResolvedPointTable(
                    protocol="ads",
                    points=[_make_point("p1"), _make_point("p2")],
                )
            }
            new_cfg = _full_config(devices=devices, tasks=[task], tables=new_tables)
            diff = ConfigDiff(points_changed=True, point_tables_changed=["t1"])

            errors = await rt.reconfigure(new_cfg, diff)

            assert errors == []
            assert first_subscription.close.await_count == 1
            assert proto.subscribe.await_count == 2
            assert rt._acquisition_handles["t1:d1"] is not first_handle  # noqa: SLF001
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
            assert proto.connect.await_count == 1
        finally:
            await rt.stop()


    async def test_subscription_restart_failure_returns_reload_error_and_retries(self) -> None:
        """订阅重建失败不能被吞掉；下一次 reload 必须继续重试并恢复 RUNNING。"""
        devices = [_make_device_config("d1", protocol="ads")]
        task = _make_task("t1", device="d1", interval=0.02)
        rt, protos, _, _ = _build_runtime(
            devices=devices,
            tasks=[task],
            points={"d1": [_make_point("p1")]},
        )
        proto = protos["d1"]
        proto.acquisition_mode = AcquisitionMode.SUBSCRIBE

        first_subscription = MagicMock()
        first_subscription.close = AsyncMock()
        recovered_subscription = MagicMock()
        recovered_subscription.close = AsyncMock()
        proto.subscribe = AsyncMock(
            side_effect=[
                first_subscription,
                RuntimeError("register failed"),
                recovered_subscription,
            ]
        )

        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            new_tables = {
                "t1": ResolvedPointTable(
                    protocol="ads",
                    points=[_make_point("p1"), _make_point("p2")],
                )
            }
            new_cfg = _full_config(devices=devices, tasks=[task], tables=new_tables)
            diff = ConfigDiff(points_changed=True, point_tables_changed=["t1"])

            errors1 = await rt.reconfigure(new_cfg, diff)

            assert errors1
            assert "tasks:" in errors1[0]
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.STOPPED
            assert "t1:d1" in rt._restart_pending  # noqa: SLF001
            assert proto.subscribe.await_count == 2

            errors2 = await rt.reconfigure(new_cfg, diff)

            assert errors2 == []
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
            assert "t1:d1" not in rt._restart_pending  # noqa: SLF001
            assert proto.subscribe.await_count == 3
        finally:
            await rt.stop()


    async def test_point_table_binding_switch_restarts_running_subscription_handle(self) -> None:
        """设备切换到另一张既有点表时也必须重新注册订阅。"""
        d1 = _make_device_config("d1", protocol="ads", point_table="t1")
        task = _make_task("t1", device="d1", interval=0.02)
        points = {"d1": [_make_point("p1")]}
        rt, protos, _, _ = _build_runtime(devices=[d1], tasks=[task], points=points)
        proto = protos["d1"]
        proto.acquisition_mode = AcquisitionMode.SUBSCRIBE

        first_subscription = MagicMock()
        first_subscription.close = AsyncMock()
        second_subscription = MagicMock()
        second_subscription.close = AsyncMock()
        proto.subscribe = AsyncMock(side_effect=[first_subscription, second_subscription])

        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            d1_new = _make_device_config("d1", protocol="ads", point_table="t2")
            tables = {
                "t1": ResolvedPointTable(protocol="ads", points=[_make_point("p1")]),
                "t2": ResolvedPointTable(protocol="ads", points=[_make_point("p2")]),
            }
            new_cfg = _full_config(devices=[d1_new], tasks=[task], tables=tables)
            diff = ConfigDiff(devices=DeviceDiff(updated=["d1"]))

            errors = await rt.reconfigure(new_cfg, diff)

            assert errors == []
            assert first_subscription.close.await_count == 1
            assert proto.subscribe.await_count == 2
            assert rt.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
            assert [p.point_id for p in rt.devices["d1"].points] == ["p2"]
            assert proto.connect.await_count == 1
        finally:
            await rt.stop()


# ---------------------------------------------------------------------------
# Sink 派发与背压
# ---------------------------------------------------------------------------


class TestSinkDispatch:
    async def test_targets_fan_out_to_all_sinks(self) -> None:
        """真实引擎：task.targets 决定输出——同一批次派发到全部 target sink。"""
        devices = [_make_device_config("d1")]
        protos = {"d1": _mock_protocol()}
        protos["d1"].read = AsyncMock(return_value=[_value("d1", "p1")])
        sinks = {"s1": _mock_sink(), "s2": _mock_sink()}
        points = {"d1": [_make_point("p1", groups=("g1",))]}
        device_map = _build_devices(devices, protos, points)
        engine = AcquisitionEngine(read_timeout=None)
        rt = Runtime(
            devices=device_map,
            sinks=sinks,
            engine=engine,
            dispatcher=CommandDispatcher(device_map),
            config=_runtime_config(),
            tasks={"t1": _make_task("t1", device="d1", interval=0.02, sinks=("s1", "s2"))},
        )
        await rt.start()
        try:
            await rt.start_task_instance("t1:d1")
            await _wait_for(lambda: sinks["s1"].write.await_count >= 1, what="s1 write")
            await _wait_for(lambda: sinks["s2"].write.await_count >= 1, what="s2 write")
            batch_s1 = sinks["s1"].write.await_args[0][0]
            batch_s2 = sinks["s2"].write.await_args[0][0]
            assert [v.point_id for v in batch_s1] == ["p1"]
            assert [v.point_id for v in batch_s2] == ["p1"]
            assert rt.points_routed == 2  # 1 点 × 2 sink
            assert rt.points_collected == 1
        finally:
            await rt.stop()

    async def test_dispatch_to_unknown_sink_skipped(self) -> None:
        rt, _, _, _ = _build_runtime(devices=[], tasks=[])
        await rt.dispatch({"ghost": [_value()]})
        assert rt.points_routed == 0
        assert rt.points_dropped == 0

    async def test_backpressure_drop_old_evicts_oldest(self) -> None:
        rt, _, _, _ = _build_runtime(devices=[], tasks=[], backpressure="drop_old", queue_maxsize=1)
        await rt.dispatch({"s1": [_value(point_id="p1")]})
        await rt.dispatch({"s1": [_value(point_id="p2"), _value(point_id="p3")]})
        assert rt.points_dropped == 1  # 最旧批次被驱逐
        assert rt.points_routed == 3
        assert rt.sink_queue_depths() == {"s1": 1}

    async def test_backpressure_drop_new_discards_incoming(self) -> None:
        rt, _, _, _ = _build_runtime(devices=[], tasks=[], backpressure="drop_new", queue_maxsize=1)
        await rt.dispatch({"s1": [_value(point_id="p1")]})
        await rt.dispatch({"s1": [_value(point_id="p2"), _value(point_id="p3")]})
        assert rt.points_dropped == 2  # 新批次整体丢弃
        assert rt.points_routed == 1
        assert rt.sink_queue_depths() == {"s1": 1}

    async def test_empty_batch_ignored(self) -> None:
        rt, _, _, _ = _build_runtime(devices=[], tasks=[])
        await rt.dispatch({"s1": []})
        assert rt.points_routed == 0
        assert rt.sink_queue_depths() == {"s1": 0}

    async def test_sink_write_failure_counts_batch_as_dropped(self) -> None:
        metrics = CollectorMetricsState()
        rt, _, sinks, _ = _build_runtime(
            devices=[],
            tasks=[],
            metrics_hook=metrics,
        )
        sinks["s1"].write = AsyncMock(side_effect=RuntimeError("sink unavailable"))
        await rt.start()
        try:
            await rt.dispatch(
                {"s1": [_value(point_id="p1"), _value(point_id="p2")]}
            )
            await _wait_for(
                lambda: sinks["s1"].write.await_count == 1,
                what="sink write failure",
            )

            assert rt.points_routed == 2
            assert rt.points_dropped == 2
            events = metrics.snapshot()["events"]
            assert events[-1]["kind"] == "sink_write_failed"
            assert events[-1]["object"] == "s1"
            assert "2 point(s)" in events[-1]["message"]
        finally:
            await rt.stop()


# ---------------------------------------------------------------------------
# 健康与组件生命周期（best-effort 语义）
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_sink_open_failure_exposed_unhealthy(self) -> None:
        rt, _, sinks, _ = _build_runtime(
            devices=[_make_device_config("d1")],
            tasks=[_make_task("t1", device="d1")],
        )
        sinks["s1"].open = AsyncMock(side_effect=RuntimeError("open boom"))
        await rt.start()
        try:
            health = rt.health()
            assert health["d1"].healthy is True
            assert health["s1"].healthy is False
            assert rt.running is True  # 单组件失败不阻塞整体启动
        finally:
            await rt.stop()
