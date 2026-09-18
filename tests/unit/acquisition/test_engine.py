"""采集引擎（``domain/acquisition``）的单元测试。

验证对象：:class:`AcquisitionEngine`——单次「Protocol read → Pipeline →
Router → Sink 派发」链路。

覆盖点：

- ``collect``：按点表构造 PointRef、读取失败只记日志不上抛、未知设备跳过；
- ``process_and_route``：计数口径（决策 0.1/7）、回调、观察者通知与异常隔离、
  空批次短路、未绑定派发端口时显式失败；
- ``replace_router`` / ``replace_pipeline`` / ``current_router``。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.acquisition import AcquisitionEngine
from wind_hub.domain.model.point import PointRef, PointValue, Quality
from wind_hub.domain.model.route import DeliveryConfig, RouteRule, RouteTarget
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort
from wind_hub.domain.processing import Pipeline
from wind_hub.domain.routing import DeliveryDispatcher, Router, policies_from_rules

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


def _make_point(point_id: str, group: str = "default") -> PointConfig:
    return PointConfig(
        point_id=point_id,
        group=group,
        address=PointAddress(type="holding_register"),
    )


def _value(device_id: str = "d1", point_id: str = "p1") -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=1.0)


class _RecordingDispatch:
    """SinkDispatchPort 的测试实现——记录每次派发的路由结果。"""

    def __init__(self) -> None:
        self.dispatched: list[dict[str, list[PointValue]]] = []

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        self.dispatched.append(routed)


def _make_engine(
    protocols: dict[str, ProtocolPort] | None = None,
    points_by_device: dict[str, list[PointConfig]] | None = None,
    on_points_collected=None,
    router: Router | None = None,
) -> tuple[AcquisitionEngine, _RecordingDispatch]:
    """构造绑定好录制派发端口的引擎，返回 (engine, dispatch)。"""
    engine = AcquisitionEngine(
        protocols=protocols if protocols is not None else {},
        pipeline=Pipeline([]),
        router=router if router is not None else MagicMock(spec=Router),
        points_by_device=points_by_device,
        on_points_collected=on_points_collected,
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
    protocols = {"d1": proto}
    points = {"d1": [_make_point("p1"), _make_point("p2")]}
    engine, dispatch = _make_engine(protocols, points)

    await engine.collect("d1", "default")

    proto.read.assert_awaited_once()
    refs = proto.read.await_args.args[0]
    assert [r.point_id for r in refs] == ["p1", "p2"]
    assert all(r.device_id == "d1" for r in refs)
    assert len(dispatch.dispatched) == 1


async def test_collect_reads_only_points_of_group() -> None:
    """一个 (device, group) 对应一个 Job——collect 只读取该 group 的点。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=[_value()])
    points = {
        "d1": [
            _make_point("p1", group="fast"),
            _make_point("p2", group="slow"),
            _make_point("p3", group="fast"),
        ]
    }
    engine, _dispatch = _make_engine({"d1": proto}, points)

    await engine.collect("d1", "fast")

    refs = proto.read.await_args.args[0]
    assert [r.point_id for r in refs] == ["p1", "p3"]


async def test_collect_group_without_points_reads_nothing() -> None:
    """该 group 无点位时不调用 read、不派发。"""
    proto = _make_mock_protocol()
    points = {"d1": [_make_point("p1", group="fast")]}
    engine, dispatch = _make_engine({"d1": proto}, points)

    await engine.collect("d1", "slow")

    proto.read.assert_not_awaited()
    assert dispatch.dispatched == []


async def test_collect_unknown_device_skips() -> None:
    """无协议驱动的设备：记日志并返回，不抛异常、不派发。"""
    engine, dispatch = _make_engine()
    await engine.collect("ghost", "default")
    assert dispatch.dispatched == []


async def test_collect_read_failure_swallowed() -> None:
    """单次读取失败只记 warning 不上抛（下个周期自然重试）。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(side_effect=OSError("device unreachable"))
    engine, dispatch = _make_engine({"d1": proto}, {"d1": [_make_point("p1")]})

    await engine.collect("d1", "default")  # 不抛异常

    assert dispatch.dispatched == []
    assert engine.points_collected == 0


async def test_collect_empty_batch_counts_nothing() -> None:
    proto = _make_mock_protocol()  # read 返回 []
    engine, dispatch = _make_engine({"d1": proto}, {"d1": [_make_point("p1")]})

    await engine.collect("d1", "default")

    assert engine.points_collected == 0
    assert dispatch.dispatched == []


# ---------------------------------------------------------------------------
# process_and_route —— 处理/路由/派发与计数
# ---------------------------------------------------------------------------


async def test_process_and_route_counts_and_notifies() -> None:
    """采集计数在入口统一累加，注入回调口径一致（决策 0.1/7）。"""
    collected: list[int] = []
    engine, _dispatch = _make_engine(on_points_collected=collected.append)

    await engine.process_and_route([_value(), _value(point_id="p2")])
    await engine.process_and_route([_value(point_id="p3")])

    assert engine.points_collected == 3
    assert collected == [2, 1]


async def test_process_and_route_runs_pipeline_before_route() -> None:
    """Pipeline 先执行，Router 看到的是处理后的点值。"""
    seen_by_router: list[list[PointValue]] = []
    router = MagicMock(spec=Router)
    router.route.side_effect = lambda vals: seen_by_router.append(vals) or {}

    engine, dispatch = _make_engine()
    engine._router = router  # noqa: SLF001 — 用录制替身替换默认 mock

    batch = [_value()]
    await engine.process_and_route(batch)

    assert seen_by_router == [batch]


async def test_process_and_route_dispatch_raises_when_unbound() -> None:
    """未绑定 Sink 派发端口时显式失败——装配时序错误不得静默。"""
    engine = AcquisitionEngine(
        protocols={},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
    )
    with pytest.raises(RuntimeError, match="派发端口"):
        await engine.process_and_route([_value()])


async def test_process_and_route_empty_batch_noop() -> None:
    """空批次短路：不计数、不触发管线/派发，未绑定端口也不报错。"""
    engine = AcquisitionEngine(
        protocols={},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
    )
    await engine.process_and_route([])  # 不抛异常
    assert engine.points_collected == 0


# ---------------------------------------------------------------------------
# 观察者
# ---------------------------------------------------------------------------


async def test_observers_receive_processed_values() -> None:
    received: list[list[PointValue]] = []
    engine, _dispatch = _make_engine()
    engine.add_observer(received.append)

    batch = [_value()]
    await engine.process_and_route(batch)

    assert received == [batch]


async def test_observer_exception_isolated() -> None:
    """观察者抛异常只记日志，采集链路与其他观察者不受影响。"""

    def _bad_observer(_values: list[PointValue]) -> None:
        raise RuntimeError("observer boom")

    received: list[list[PointValue]] = []
    engine, dispatch = _make_engine()
    engine.add_observer(_bad_observer)
    engine.add_observer(received.append)

    await engine.process_and_route([_value()])

    assert len(received) == 1
    assert len(dispatch.dispatched) == 1


# ---------------------------------------------------------------------------
# 当前实例替换
# ---------------------------------------------------------------------------


async def test_replace_router_and_pipeline_take_effect_immediately() -> None:
    engine, dispatch = _make_engine()

    new_router = MagicMock(spec=Router)
    new_router.table_size = 7
    new_router.route.return_value = {"s1": [_value()]}
    await engine.replace_router(new_router)
    assert engine.current_router is new_router

    new_pipeline = Pipeline([])
    await engine.replace_pipeline(new_pipeline)

    batch = [_value()]
    await engine.process_and_route(batch)
    new_router.route.assert_called_once()
    # 路由结果原样派发（Mock 的 return_value 即被派发对象）
    assert dispatch.dispatched[-1] is new_router.route.return_value


async def test_current_router_reflects_initial_instance() -> None:
    router = MagicMock(spec=Router)
    engine = AcquisitionEngine(protocols={}, pipeline=Pipeline([]), router=router)
    assert engine.current_router is router


# ---------------------------------------------------------------------------
# 投递策略（阶段 B）——DeliveryDispatcher 接入
# ---------------------------------------------------------------------------


def _real_router_with_every_n(n: int) -> tuple[Router, DeliveryDispatcher]:
    """真实 Router + every_n 投递策略（规则 r1 匹配所有点、发往 s1）。"""
    rule = RouteRule(
        name="r1",
        targets=[RouteTarget(sink="s1", delivery=DeliveryConfig(type="every_n", n=n))],
    )
    table = RoutingTable([rule], {"d1": [_make_point("p1")]})
    router = Router(table)
    return router, DeliveryDispatcher(router, policies_from_rules([rule]))


async def test_process_and_route_applies_delivery_policy() -> None:
    """装配投递策略后：Router 结果先经 DeliveryDispatcher 过滤再派发。"""
    router, delivery = _real_router_with_every_n(n=2)
    engine, dispatch = _make_engine(router=router)
    await engine.replace_delivery(delivery)

    await engine.process_and_route([_value()])  # 第 1 批：投递
    await engine.process_and_route([_value()])  # 第 2 批：every_n=2 抑制
    await engine.process_and_route([_value()])  # 第 3 批：投递

    assert len(dispatch.dispatched) == 3  # 每批都调用了派发端口
    assert len(dispatch.dispatched[0]["s1"]) == 1
    assert dispatch.dispatched[1] == {}  # 被整批抑制
    assert len(dispatch.dispatched[2]["s1"]) == 1


async def test_replace_delivery_swaps_policy_without_touching_router() -> None:
    """replace_delivery 原子替换策略状态；Router 实例不变。"""
    router, every_two = _real_router_with_every_n(n=2)
    engine, dispatch = _make_engine(router=router)
    await engine.replace_delivery(every_two)
    router_before = engine.current_router

    # 换成 always（无策略）——此后每批都投递
    always = DeliveryDispatcher(router, {})
    await engine.replace_delivery(always)

    assert engine.current_router is router_before  # 路由表未被触碰
    for _ in range(3):
        await engine.process_and_route([_value()])
    assert all(d.get("s1") for d in dispatch.dispatched)


async def test_no_delivery_means_route_equals_dispatch() -> None:
    """未装配投递策略（delivery=None）时保持旧行为：路由结果原样派发。"""
    router, _ = _real_router_with_every_n(n=100)
    engine, dispatch = _make_engine(router=router)  # 策略不接线

    await engine.process_and_route([_value()])
    await engine.process_and_route([_value()])

    assert all(d.get("s1") for d in dispatch.dispatched)


# ---------------------------------------------------------------------------
# 应用层读超时（外层 asyncio.wait_for 兜底）
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
    """AcquisitionStatePort 录制实现——按调用顺序记录事件。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, object]] = []

    def report_collect_started(self, device_id: str, group: str) -> None:
        self.events.append(("started", device_id, group, None))

    def report_collect_success(self, device_id: str, group: str, *, partial: bool) -> None:
        self.events.append(("success", device_id, group, partial))

    def report_collect_failure(self, device_id: str, group: str, error: str) -> None:
        self.events.append(("failure", device_id, group, error))


def _make_engine_with_states(
    proto: ProtocolPort,
    points: list[PointConfig],
    *,
    read_timeout: float | None = None,
    ensure_result: bool = True,
    on_points_bad=None,
) -> tuple[AcquisitionEngine, _RecordingDispatch, _RecordingDeviceState, _RecordingAcqState]:
    """构造绑定录制版 DeviceStatePort / AcquisitionStatePort 的引擎。"""
    engine, dispatch = _make_engine({"d1": proto}, {"d1": points})
    if read_timeout is not None:
        engine._read_timeout = read_timeout  # noqa: SLF001 — 等价于构造注入
    device_state = _RecordingDeviceState(ensure_result)
    acq_state = _RecordingAcqState()
    engine.attach_device_state(device_state)
    engine.attach_acquisition_state(acq_state)
    if on_points_bad is not None:
        engine._on_points_bad = on_points_bad  # noqa: SLF001 — 等价于构造注入
    return engine, dispatch, device_state, acq_state


async def test_collect_read_timeout_reports_failure_and_marks_device() -> None:
    """外层读超时：collect 不抛出、不派发；acq 记 failure（read timeout），
    设备状态收到连接级 TimeoutError（触发断线/重连路径）。"""
    hang = asyncio.Event()  # 永不 set——read 挂起直到外层超时

    async def _hanging_read(_refs: list[PointRef]) -> list[PointValue]:
        await hang.wait()
        return []  # pragma: no cover — 永远不会到达

    proto = _make_mock_protocol()
    proto.read = AsyncMock(side_effect=_hanging_read)
    engine, dispatch, device_state, acq_state = _make_engine_with_states(
        proto, [_make_point("p1")], read_timeout=0.05
    )

    await engine.collect("d1", "default")  # 不抛异常

    assert dispatch.dispatched == []
    assert engine.points_collected == 0
    # acq 生命周期：started → failure（错误语义定位到 read 阶段）
    assert [e[0] for e in acq_state.events] == ["started", "failure"]
    error = acq_state.events[-1][3]
    assert isinstance(error, str)
    assert "read timeout" in error
    # 设备状态：读超时按连接级失败上报（TimeoutError）
    assert len(device_state.read_failures) == 1
    assert isinstance(device_state.read_failures[0], TimeoutError)


async def test_collect_without_read_timeout_keeps_driver_timeout_message() -> None:
    """未配置外层超时、驱动自身抛 TimeoutError：沿用驱动消息，不格式化 None。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(side_effect=TimeoutError("socket timed out"))
    engine, _dispatch, _device_state, acq_state = _make_engine_with_states(
        proto, [_make_point("p1")]
    )

    await engine.collect("d1", "default")

    assert [e[0] for e in acq_state.events] == ["started", "failure"]
    assert acq_state.events[-1][3] == "socket timed out"


async def test_collect_disconnected_reports_failed_without_read() -> None:
    """断线且重连节流中（ensure_connected=False）：本次 FAILED，不重发 read。"""
    proto = _make_mock_protocol()
    engine, dispatch, _device_state, acq_state = _make_engine_with_states(
        proto, [_make_point("p1")], ensure_result=False
    )

    await engine.collect("d1", "default")

    proto.read.assert_not_awaited()
    assert dispatch.dispatched == []
    assert [e[0] for e in acq_state.events] == ["started", "failure"]
    assert "disconnected" in str(acq_state.events[-1][3])


async def test_collect_pipeline_exception_reports_failure_and_reraises() -> None:
    """管线/派发阶段异常：acq 记 failure 后原样上抛（保持既有传播语义），
    running 状态由 finish_failure 归位（不重复上报）。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=[_value()])
    engine, _dispatch, _device_state, acq_state = _make_engine_with_states(
        proto, [_make_point("p1")]
    )

    class _ExplodingDispatch:
        async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
            raise RuntimeError("sink exploded")

    engine.attach_sink_dispatch(_ExplodingDispatch())

    with pytest.raises(RuntimeError, match="sink exploded"):
        await engine.collect("d1", "default")

    # 恰好一次 started + 一次 failure（无双报）
    assert [e[0] for e in acq_state.events] == ["started", "failure"]
    assert "sink exploded" in str(acq_state.events[-1][3])


# ---------------------------------------------------------------------------
# 批量读部分失败语义（§3/§4）
# ---------------------------------------------------------------------------


def _bad_value(device_id: str = "d1", point_id: str = "p2") -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=None, quality=Quality.BAD)


async def test_collect_partial_batch_keeps_order_and_reports_partial() -> None:
    """GOOD/BAD/GOOD 混合批：三点全量按序进入派发，BAD 值 None、GOOD 不受影响；
    acq 判定 success(partial=True)——不计连续失败。"""
    batch = [_value(point_id="p1"), _bad_value(point_id="p2"), _value(point_id="p3")]
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=batch)
    bad_counts: list[int] = []
    engine, dispatch, device_state, acq_state = _make_engine_with_states(
        proto,
        [_make_point("p1"), _make_point("p2"), _make_point("p3")],
        on_points_bad=bad_counts.append,
    )

    await engine.collect("d1", "default")

    # 批次三点全量保留、顺序不变
    assert len(dispatch.dispatched) == 1
    router = engine.current_router
    seen = router.route.call_args.args[0]
    assert [v.point_id for v in seen] == ["p1", "p2", "p3"]
    assert seen[1].value is None
    assert seen[1].quality is Quality.BAD
    assert seen[0].quality is Quality.GOOD
    assert seen[2].quality is Quality.GOOD
    # 计数口径：points_collected 含 BAD（数据质量信息流向 sink），
    # on_points_bad 单独记 1 个 BAD 点
    assert engine.points_collected == 3
    assert bad_counts == [1]
    # acq 判定：success 且 partial=True；设备读成功
    assert [e[0] for e in acq_state.events] == ["started", "success"]
    assert acq_state.events[-1][3] is True
    assert device_state.read_successes == 1


async def test_collect_all_bad_batch_reports_failure() -> None:
    """全 BAD 批 = 没有任何有效结果 → FAILED；批次仍流向 sink（质量信息）。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(return_value=[_bad_value(point_id="p1")])
    engine, dispatch, _device_state, acq_state = _make_engine_with_states(
        proto, [_make_point("p1")]
    )

    await engine.collect("d1", "default")

    assert [e[0] for e in acq_state.events] == ["started", "failure"]
    assert "no valid values" in str(acq_state.events[-1][3])
    assert len(dispatch.dispatched) == 1  # BAD 批次照常派发


async def test_real_pipeline_processors_tolerate_bad_values() -> None:
    """真实内置管线（quality_check/unit_convert/deadband）吃 BAD 点不崩溃：
    BAD 透传、不参与死区比较也不污染死区状态，GOOD 点正常处理。"""
    from wind_hub.adapter.outbound.processor.builtin.deadband import DeadbandProcessor
    from wind_hub.adapter.outbound.processor.builtin.quality_check import (
        QualityCheckProcessor,
    )
    from wind_hub.adapter.outbound.processor.builtin.unit_convert import UnitConvertProcessor

    points = [
        PointConfig(
            point_id="p1",
            group="default",
            address=PointAddress(type="holding_register"),
            data_type="float32",
            deadband=0.5,
        ),
        PointConfig(
            point_id="p2",
            group="default",
            address=PointAddress(type="holding_register"),
            data_type="float32",
            deadband=0.5,
        ),
    ]
    qc = QualityCheckProcessor()
    uc = UnitConvertProcessor()
    db = DeadbandProcessor()
    for proc in (qc, uc, db):
        proc.set_points_config({"d1": points})

    engine, _dispatch = _make_engine({"d1": _make_mock_protocol()}, {"d1": points})
    await engine.replace_pipeline(Pipeline([qc, uc, db]))

    # 第一批：GOOD 1.0（首次，记录死区基线）+ BAD（透传，不进死区状态）
    await engine.process_and_route([_value(point_id="p1"), _bad_value(point_id="p2")])
    seen = engine.current_router.route.call_args.args[0]
    assert len(seen) == 2
    assert seen[1].quality is Quality.BAD
    assert seen[1].value is None

    # 第二批：p1 变化 0.1 < deadband 0.5 → 被死区丢弃（BAD 未污染基线）
    await engine.process_and_route([PointValue(device_id="d1", point_id="p1", value=1.1)])
    seen2 = engine.current_router.route.call_args.args[0]
    assert seen2 == []

    # 第三批：p2 首次 GOOD 值——此前 BAD 没有建立基线，按首次遇见输出
    await engine.process_and_route([PointValue(device_id="d1", point_id="p2", value=9.0)])
    seen3 = engine.current_router.route.call_args.args[0]
    assert [v.point_id for v in seen3] == ["p2"]
