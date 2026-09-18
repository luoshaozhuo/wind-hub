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

from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.acquisition import AcquisitionEngine
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort
from wind_hub.domain.processing import Pipeline
from wind_hub.domain.routing import Router

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


def _make_point(device_id: str, point_id: str) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        device_id=device_id,
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
) -> tuple[AcquisitionEngine, _RecordingDispatch]:
    """构造绑定好录制派发端口的引擎，返回 (engine, dispatch)。"""
    engine = AcquisitionEngine(
        protocols=protocols if protocols is not None else {},
        pipeline=Pipeline([]),
        router=MagicMock(spec=Router),
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
    points = {"d1": [_make_point("d1", "p1"), _make_point("d1", "p2")]}
    engine, dispatch = _make_engine(protocols, points)

    await engine.collect("d1", "default")

    proto.read.assert_awaited_once()
    refs = proto.read.await_args.args[0]
    assert [r.point_id for r in refs] == ["p1", "p2"]
    assert all(r.device_id == "d1" for r in refs)
    assert len(dispatch.dispatched) == 1


async def test_collect_unknown_device_skips() -> None:
    """无协议驱动的设备：记日志并返回，不抛异常、不派发。"""
    engine, dispatch = _make_engine()
    await engine.collect("ghost", "default")
    assert dispatch.dispatched == []


async def test_collect_read_failure_swallowed() -> None:
    """单次读取失败只记 warning 不上抛（下个周期自然重试）。"""
    proto = _make_mock_protocol()
    proto.read = AsyncMock(side_effect=OSError("device unreachable"))
    engine, dispatch = _make_engine({"d1": proto}, {"d1": [_make_point("d1", "p1")]})

    await engine.collect("d1", "default")  # 不抛异常

    assert dispatch.dispatched == []
    assert engine.points_collected == 0


async def test_collect_empty_batch_counts_nothing() -> None:
    proto = _make_mock_protocol()  # read 返回 []
    engine, dispatch = _make_engine({"d1": proto}, {"d1": [_make_point("d1", "p1")]})

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
