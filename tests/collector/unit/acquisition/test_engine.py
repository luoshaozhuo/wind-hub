"""采集引擎（``domain/acquisition``）的单元测试。

验证对象：:class:`AcquisitionEngine`——单次「Device read → 按 Task
targets 扇出到 Sink」链路（无 Router / Delivery / Pipeline）。

覆盖点：

- ``collect(device, point_group, targets, execution_id)``：设备经
  ``point_refs(point_group)`` 选点构造 PointRef、读取失败只记日志不上抛、
  无点位跳过；
- ``process(batch, targets)``：sink fan-out（batch 按 targets 列表扇出，
  dispatch 收到 ``{sink_name: batch}``）、空 targets 派发空 dict、计数口径、
  回调、观察者通知与异常隔离、空批次短路、未绑定派发端口显式失败；
- ``AcquisitionStatePort`` 生命周期上报（首参为 execution_id）；
- 读超时 / 断线跳过 / 部分失败（GOOD+BAD 混合 / 全 BAD）语义。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub_core.config.schema import PointAddress, PointConfig
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.model.point import PointRef, PointValue, Quality
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.protocol.port import ProtocolPort

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_protocol() -> ProtocolPort:
    proto = MagicMock(spec=ProtocolPort)
    proto.set_points_mapping = MagicMock()
    proto.connect = AsyncMock()
    proto.close = AsyncMock()
    proto.read = AsyncMock(return_value=[])
    proto.write = AsyncMock()
    proto.subscribe = AsyncMock()
    proto.health = MagicMock(return_value=HealthStatus(healthy=True))
    return proto


class _FakeDevice:
    """结构化满足引擎 ``ReadableDevice`` 依赖的测试设备——包装 mock 协议
    与点表，按 ``point_group in p.point_groups`` 选点。"""

    def __init__(self, device_id: str, proto: ProtocolPort, points: list[PointConfig]) -> None:
        self._device_id = device_id
        self._proto = proto
        self._points = points

    @property
    def device_id(self) -> str:
        return self._device_id

    def point_refs(self, point_group: str) -> list[PointRef]:
        return [
            PointRef(device_id=self._device_id, point_id=p.point_id)
            for p in self._points
            if point_group in p.point_groups
        ]

    async def read(self, point_group: str) -> list[PointValue]:
        return await self._proto.read(self.point_refs(point_group))


def _make_point(point_id: str, point_groups: list[str] | None = None) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        point_groups=point_groups if point_groups is not None else ["default"],
        address=PointAddress(type="holding_register"),
    )


def _value(device_id: str = "d1", point_id: str = "p1") -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=1.0)


def _bad_value(device_id: str = "d1", point_id: str = "p2") -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=None, quality=Quality.BAD)


class _RecordingDispatch:
    """SinkDispatchPort 的测试实现——记录每次派发的扇出结果。"""

    def __init__(self) -> None:
        self.dispatched: list[dict[str, list[PointValue]]] = []

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        self.dispatched.append(routed)


def _make_engine(
    on_points_collected=None,
    on_points_bad=None,
    read_timeout: float | None = None,
) -> tuple[AcquisitionEngine, _RecordingDispatch]:
    """构造绑定好录制派发端口的引擎，返回 (engine, dispatch)。"""
    engine = AcquisitionEngine(
        on_points_collected=on_points_collected,
        on_points_bad=on_points_bad,
        read_timeout=read_timeout,
    )
    dispatch = _RecordingDispatch()
    engine.attach_sink_dispatch(dispatch)
    return engine, dispatch


# ---------------------------------------------------------------------------
# collect —— 轮询执行体
# ---------------------------------------------------------------------------


async def test_collect_reads_point_refs_from_table() -> None:
    """collect 按设备点表构造 PointRef 调用 read，结果进入完整链路。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=[_value()])
    device = _FakeDevice("d1", proto, [_make_point("p1"), _make_point("p2")])
    engine, dispatch = _make_engine()

    await engine.collect(device, "default", ["s1"], "task-1:d1")

    proto.read.assert_awaited_once()
    refs = proto.read.await_args.args[0]
    assert [r.point_id for r in refs] == ["p1", "p2"]
    assert all(r.device_id == "d1" for r in refs)
    assert len(dispatch.dispatched) == 1


async def test_collect_reads_only_points_of_group() -> None:
    """collect 只读取 ``point_groups`` 含目标 point_group 的点。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=[_value()])
    device = _FakeDevice(
        "d1",
        proto,
        [
            _make_point("p1", ["fast"]),
            _make_point("p2", ["slow"]),
            _make_point("p3", ["fast"]),
        ],
    )
    engine, _dispatch = _make_engine()

    await engine.collect(device, "fast", ["s1"], "task-1:d1")

    refs = proto.read.await_args.args[0]
    assert [r.point_id for r in refs] == ["p1", "p3"]


async def test_collect_point_in_multiple_groups_selected_by_each() -> None:
    """point_groups 多值：同一个点可被多个分组命中——不同 collect 各自选中它。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=[_value()])
    device = _FakeDevice(
        "d1",
        proto,
        [
            _make_point("p1", ["fast", "alarm"]),  # 多分组点
            _make_point("p2", ["alarm"]),
            _make_point("p3", ["fast"]),
        ],
    )
    engine, _dispatch = _make_engine()

    await engine.collect(device, "fast", ["s1"], "t-fast:d1")
    refs_fast = proto.read.await_args.args[0]
    assert [r.point_id for r in refs_fast] == ["p1", "p3"]

    await engine.collect(device, "alarm", ["s1"], "t-alarm:d1")
    refs_alarm = proto.read.await_args.args[0]
    assert [r.point_id for r in refs_alarm] == ["p1", "p2"]


async def test_collect_group_without_points_reads_nothing() -> None:
    """该 point_group 无点位时不调用 read、不派发。"""
    proto = _make_mock_protocol()
    device = _FakeDevice("d1", proto, [_make_point("p1", ["fast"])])
    engine, dispatch = _make_engine()

    await engine.collect(device, "slow", ["s1"], "t1:d1")

    proto.read.assert_not_awaited()
    assert dispatch.dispatched == []


async def test_collect_read_failure_swallowed() -> None:
    """单次读取失败只记 warning 不上抛（下个周期自然重试）。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(side_effect=OSError("device unreachable"))
    device = _FakeDevice("d1", proto, [_make_point("p1")])
    engine, dispatch = _make_engine()

    await engine.collect(device, "default", ["s1"], "t1:d1")  # 不抛异常

    assert dispatch.dispatched == []
    assert engine.points_collected == 0


async def test_collect_empty_batch_counts_nothing() -> None:
    proto = _make_mock_protocol()  # read 返回 []
    device = _FakeDevice("d1", proto, [_make_point("p1")])
    engine, dispatch = _make_engine()

    await engine.collect(device, "default", ["s1"], "t1:d1")

    assert engine.points_collected == 0
    assert dispatch.dispatched == []


# ---------------------------------------------------------------------------
# process —— sink fan-out 与计数
# ---------------------------------------------------------------------------


async def test_process_fans_out_to_multiple_sinks() -> None:
    """batch 按 targets 列表扇出：dispatch 收到 {sink_name: batch}，每个 sink
    拿到同一批处理后的点值。"""
    engine, dispatch = _make_engine()

    batch = [_value(), _value(point_id="p2")]
    await engine.process(batch, ["s1", "s2", "s3"])

    assert len(dispatch.dispatched) == 1
    routed = dispatch.dispatched[0]
    assert set(routed) == {"s1", "s2", "s3"}
    for sink in ("s1", "s2", "s3"):
        assert [v.point_id for v in routed[sink]] == ["p1", "p2"]


async def test_process_empty_targets_dispatches_empty_dict() -> None:
    """空 targets：批次照常处理，dispatch 收到空 dict（以源码语义为准）。"""
    engine, dispatch = _make_engine()

    await engine.process([_value()], [])

    assert dispatch.dispatched == [{}]
    assert engine.points_collected == 1


async def test_process_counts_and_notifies() -> None:
    """采集计数在入口统一累加，注入回调口径一致。"""
    collected: list[int] = []
    engine, _dispatch = _make_engine(on_points_collected=collected.append)

    await engine.process([_value(), _value(point_id="p2")], ["s1"])
    await engine.process([_value(point_id="p3")], ["s1"])

    assert engine.points_collected == 3
    assert collected == [2, 1]


async def test_process_bad_points_counted_separately() -> None:
    """BAD 质量点计入 points_collected（质量信息流向 sink），同时单独经
    on_points_bad 计数。"""
    bad_counts: list[int] = []
    engine, _dispatch = _make_engine(on_points_bad=bad_counts.append)

    await engine.process([_value(), _bad_value()], ["s1"])

    assert engine.points_collected == 2
    assert bad_counts == [1]


async def test_process_dispatches_batch_unchanged() -> None:
    """引擎不做任何值变换——dispatch 拿到的就是传入的批次对象。"""
    engine, dispatch = _make_engine()

    batch = [_value()]
    await engine.process(batch, ["s1"])

    routed = dispatch.dispatched[0]
    assert routed["s1"] is batch


async def test_process_raises_when_unbound() -> None:
    """未绑定 Sink 派发端口时显式失败——装配时序错误不得静默。"""
    engine = AcquisitionEngine()
    with pytest.raises(RuntimeError, match="派发端口"):
        await engine.process([_value()], ["s1"])


async def test_process_empty_batch_noop() -> None:
    """空批次短路：不计数、不触发管线/派发，未绑定端口也不报错。"""
    engine = AcquisitionEngine()
    await engine.process([], ["s1"])  # 不抛异常
    assert engine.points_collected == 0


# ---------------------------------------------------------------------------
# 观察者
# ---------------------------------------------------------------------------


async def test_observers_receive_processed_values() -> None:
    received: list[list[PointValue]] = []
    engine, _dispatch = _make_engine()
    engine.add_observer(received.append)

    batch = [_value()]
    await engine.process(batch, ["s1"])

    assert received == [batch]


async def test_observer_exception_isolated() -> None:
    """观察者抛异常只记日志，采集链路与其他观察者不受影响。"""

    def _bad_observer(_values: list[PointValue]) -> None:
        raise RuntimeError("observer boom")

    received: list[list[PointValue]] = []
    engine, dispatch = _make_engine()
    engine.add_observer(_bad_observer)
    engine.add_observer(received.append)

    await engine.process([_value()], ["s1"])

    assert len(received) == 1
    assert len(dispatch.dispatched) == 1


# ---------------------------------------------------------------------------
# 状态端口（DeviceState / AcquisitionState）与读超时
# ---------------------------------------------------------------------------


class _RecordingDeviceState:
    """DeviceStatePort 录制实现——记录 ensure/report 调用。"""

    def __init__(self, ensure_result: bool = True) -> None:
        self.ensure_result = ensure_result
        self.read_failures: list[BaseException] = []
        self.read_successes = 0

    async def ensure_connected(self, device_id: str) -> bool:
        return self.ensure_result

    def report_read_success(self, device_id: str) -> None:
        self.read_successes += 1

    def report_read_failure(self, device_id: str, error: BaseException) -> None:
        self.read_failures.append(error)


class _RecordingAcqState:
    """AcquisitionStatePort 录制实现——按调用顺序记录事件（首参 execution_id）。"""

    def __init__(self) -> None:
        # (event, execution_id, device_id, group, extra)
        self.events: list[tuple[str, str, str, str, object]] = []

    def report_collect_started(self, execution_id: str, device_id: str, group: str) -> None:
        self.events.append(("started", execution_id, device_id, group, None))

    def report_collect_success(
        self, execution_id: str, device_id: str, group: str, *, partial: bool
    ) -> None:
        self.events.append(("success", execution_id, device_id, group, partial))

    def report_collect_failure(
        self, execution_id: str, device_id: str, group: str, error: str
    ) -> None:
        self.events.append(("failure", execution_id, device_id, group, error))


def _make_engine_with_states(
    proto: ProtocolPort,
    points: list[PointConfig],
    *,
    read_timeout: float | None = None,
    ensure_result: bool = True,
    on_points_bad=None,
) -> tuple[
    AcquisitionEngine,
    _RecordingDispatch,
    _RecordingDeviceState,
    _RecordingAcqState,
    _FakeDevice,
]:
    """构造绑定录制版 DeviceStatePort / AcquisitionStatePort 的引擎与设备。"""
    engine, dispatch = _make_engine(
        read_timeout=read_timeout,
        on_points_bad=on_points_bad,
    )
    device_state = _RecordingDeviceState(ensure_result)
    acq_state = _RecordingAcqState()
    engine.attach_device_state(device_state)
    engine.attach_acquisition_state(acq_state)
    device = _FakeDevice("d1", proto, points)
    return engine, dispatch, device_state, acq_state, device


async def test_collect_acq_state_keyed_by_execution_id() -> None:
    """采集状态按 execution_id 区分：同一 (device, point_group) 被两个 Task
    采集时，各自的生命周期事件携带各自的 execution_id。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=[_value()])
    engine, _dispatch, _device_state, acq_state, device = _make_engine_with_states(
        proto, [_make_point("p1")]
    )

    await engine.collect(device, "default", ["s1"], "task-a:d1")
    await engine.collect(device, "default", ["s2"], "task-b:d1")

    assert [e[0] for e in acq_state.events] == ["started", "success", "started", "success"]
    assert acq_state.events[0][1] == "task-a:d1"
    assert acq_state.events[1][1] == "task-a:d1"
    assert acq_state.events[2][1] == "task-b:d1"
    assert acq_state.events[3][1] == "task-b:d1"
    # device_id / group 如实传递
    assert all(e[2] == "d1" and e[3] == "default" for e in acq_state.events)


async def test_collect_read_timeout_reports_failure_and_marks_device() -> None:
    """外层读超时：collect 不抛出、不派发；acq 记 failure（read timeout），
    设备状态收到连接级 TimeoutError（触发断线/重连路径）。"""
    hang = asyncio.Event()  # 永不 set——read 挂起直到外层超时

    async def _hanging_read(_refs: list[PointRef]) -> list[PointValue]:
        await hang.wait()
        return []  # pragma: no cover — 永远不会到达

    proto = _make_mock_protocol()
    proto.read = AsyncMock(side_effect=_hanging_read)
    engine, dispatch, device_state, acq_state, device = _make_engine_with_states(
        proto, [_make_point("p1")], read_timeout=0.05
    )

    await engine.collect(device, "default", ["s1"], "t1:d1")  # 不抛异常

    assert dispatch.dispatched == []
    assert engine.points_collected == 0
    # acq 生命周期：started → failure（错误语义定位到 read 阶段）
    assert [e[0] for e in acq_state.events] == ["started", "failure"]
    error = acq_state.events[-1][4]
    assert isinstance(error, str)
    assert "read timeout" in error
    # 设备状态：读超时按连接级失败上报（TimeoutError）
    assert len(device_state.read_failures) == 1
    assert isinstance(device_state.read_failures[0], TimeoutError)


async def test_collect_without_read_timeout_keeps_driver_timeout_message() -> None:
    """未配置外层超时、驱动自身抛 TimeoutError：沿用驱动消息，不格式化 None。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(side_effect=TimeoutError("socket timed out"))
    engine, _dispatch, _device_state, acq_state, device = _make_engine_with_states(
        proto, [_make_point("p1")]
    )

    await engine.collect(device, "default", ["s1"], "t1:d1")

    assert [e[0] for e in acq_state.events] == ["started", "failure"]
    assert acq_state.events[-1][4] == "socket timed out"


async def test_collect_disconnected_reports_failed_without_read() -> None:
    """断线且重连节流中（ensure_connected=False）：本次 FAILED，不重发 read。"""
    proto = _make_mock_protocol()
    engine, dispatch, _device_state, acq_state, device = _make_engine_with_states(
        proto, [_make_point("p1")], ensure_result=False
    )

    await engine.collect(device, "default", ["s1"], "t1:d1")

    proto.read.assert_not_awaited()
    assert dispatch.dispatched == []
    assert [e[0] for e in acq_state.events] == ["started", "failure"]
    assert "disconnected" in str(acq_state.events[-1][4])


async def test_collect_dispatch_exception_reports_failure_and_reraises() -> None:
    """派发阶段异常：acq 记 failure 后原样上抛（保持既有传播语义）。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=[_value()])
    engine, _dispatch, _device_state, acq_state, device = _make_engine_with_states(
        proto, [_make_point("p1")]
    )

    class _ExplodingDispatch:
        async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
            raise RuntimeError("sink exploded")

    engine.attach_sink_dispatch(_ExplodingDispatch())

    with pytest.raises(RuntimeError, match="sink exploded"):
        await engine.collect(device, "default", ["s1"], "t1:d1")

    # 恰好一次 started + 一次 failure（无双报）
    assert [e[0] for e in acq_state.events] == ["started", "failure"]
    assert "sink exploded" in str(acq_state.events[-1][4])


# ---------------------------------------------------------------------------
# 批量读部分失败语义
# ---------------------------------------------------------------------------


async def test_collect_partial_batch_keeps_order_and_reports_partial() -> None:
    """GOOD/BAD/GOOD 混合批：三点全量按序进入派发，BAD 值 None、GOOD 不受影响；
    acq 判定 success(partial=True)——不计连续失败。"""
    batch = [_value(point_id="p1"), _bad_value(point_id="p2"), _value(point_id="p3")]
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=batch)
    bad_counts: list[int] = []
    engine, dispatch, device_state, acq_state, device = _make_engine_with_states(
        proto,
        [_make_point("p1"), _make_point("p2"), _make_point("p3")],
        on_points_bad=bad_counts.append,
    )

    await engine.collect(device, "default", ["s1", "s2"], "t1:d1")

    # 批次三点全量保留、顺序不变，且扇出到两个 sink
    assert len(dispatch.dispatched) == 1
    routed = dispatch.dispatched[0]
    assert set(routed) == {"s1", "s2"}
    seen = routed["s1"]
    assert [v.point_id for v in seen] == ["p1", "p2", "p3"]
    assert seen[1].value is None
    assert seen[1].quality is Quality.BAD
    assert seen[0].quality is Quality.GOOD
    assert seen[2].quality is Quality.GOOD
    # 计数口径：points_collected 含 BAD，on_points_bad 单独记 1 个 BAD 点
    assert engine.points_collected == 3
    assert bad_counts == [1]
    # acq 判定：success 且 partial=True；设备读成功
    assert [e[0] for e in acq_state.events] == ["started", "success"]
    assert acq_state.events[-1][4] is True
    assert device_state.read_successes == 1


async def test_collect_all_bad_batch_reports_failure() -> None:
    """全 BAD 批 = 没有任何有效结果 → FAILED；批次仍流向 sink（质量信息）。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=[_bad_value(point_id="p1")])
    engine, dispatch, _device_state, acq_state, device = _make_engine_with_states(
        proto, [_make_point("p1")]
    )

    await engine.collect(device, "default", ["s1"], "t1:d1")

    assert [e[0] for e in acq_state.events] == ["started", "failure"]
    assert "no valid values" in str(acq_state.events[-1][4])
    assert len(dispatch.dispatched) == 1  # BAD 批次照常派发
