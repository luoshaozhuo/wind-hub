"""新 Collector AcquisitionEngine 语义单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from collector.domain.acquisition import AcquisitionEngine
from collector.domain.point_value import PointValue
from core.application import ProtocolSample, Quality
from tests.support.new_collector import CollectorFakeProtocol, make_collector_config, make_session


class _RecordingPorts:
    """记录式 DeviceState/AcquisitionState/SinkDispatch 三端口。"""

    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.dispatched: list[dict[str, list[PointValue]]] = []
        self.read_success: list[str] = []
        self.read_failure: list[str] = []
        self.started: list[str] = []
        self.success: list[tuple[str, bool]] = []
        self.failure: list[tuple[str, str]] = []

    async def ensure_connected(self, device_id: str) -> bool:
        return self.connected

    def report_read_success(self, device_id: str) -> None:
        self.read_success.append(device_id)

    def report_read_failure(self, device_id: str, error: BaseException) -> None:
        self.read_failure.append(device_id)

    def report_collect_started(self, execution_id: str, device_id: str, group: str) -> None:
        self.started.append(execution_id)

    def report_collect_success(
        self, execution_id: str, device_id: str, group: str, *, partial: bool
    ) -> None:
        self.success.append((execution_id, partial))

    def report_collect_failure(
        self, execution_id: str, device_id: str, group: str, error: str
    ) -> None:
        self.failure.append((execution_id, error))

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        self.dispatched.append(routed)


def _engine(ports: _RecordingPorts, **kwargs) -> AcquisitionEngine:
    engine = AcquisitionEngine(**kwargs)
    engine.attach_sink_dispatch(ports)
    engine.attach_device_state(ports)
    engine.attach_acquisition_state(ports)
    return engine


def _session(**kwargs):
    return make_session(make_collector_config(**kwargs), CollectorFakeProtocol())


async def test_collect_success_dispatches_to_targets():
    ports = _RecordingPorts()
    engine = _engine(ports)
    await engine.collect(_session(), "g", ["s1", "s2"], "t1:dev1")

    assert ports.success == [("t1:dev1", False)]
    assert ports.read_success == ["dev1"]
    assert len(ports.dispatched) == 1
    routed = ports.dispatched[0]
    assert set(routed) == {"s1", "s2"}
    batch = routed["s1"]
    assert batch[0].device_id == "dev1" and batch[0].point_id == "p1"
    assert engine.points_collected == 1


async def test_collect_empty_group_skips_without_state_reports():
    ports = _RecordingPorts()
    engine = _engine(ports)
    await engine.collect(_session(), "no-such-group", ["s1"], "t1:dev1")
    assert ports.started == [] and ports.dispatched == []


async def test_collect_disconnected_device_fails_without_read():
    ports = _RecordingPorts(connected=False)
    engine = _engine(ports)
    await engine.collect(_session(), "g", ["s1"], "t1:dev1")
    assert ports.failure == [("t1:dev1", "device disconnected (reconnect backoff)")]
    assert ports.read_success == [] and ports.dispatched == []


async def test_collect_read_exception_reports_failure_and_no_dispatch():
    ports = _RecordingPorts()
    engine = _engine(ports)
    session = _session()
    session._protocol.read_error = ConnectionRefusedError("down")  # type: ignore[attr-defined]
    await engine.collect(session, "g", ["s1"], "t1:dev1")
    assert len(ports.failure) == 1 and "down" in ports.failure[0][1]
    assert ports.read_failure == ["dev1"]
    assert ports.dispatched == []


async def test_collect_all_bad_batch_is_failure_but_still_dispatched():
    ports = _RecordingPorts()
    engine = _engine(ports)
    session = _session()

    async def bad_read(point_ids):
        return tuple(
            ProtocolSample(point_id=pid, value=None, quality=Quality.BAD) for pid in point_ids
        )

    session._protocol.read = bad_read  # type: ignore[method-assign]
    await engine.collect(session, "g", ["s1"], "t1:dev1")
    assert ports.failure == [("t1:dev1", "no valid values (1/1 BAD)")]
    assert len(ports.dispatched) == 1  # BAD 批次照常派发


async def test_collect_mixed_quality_is_partial_success():
    ports = _RecordingPorts()
    engine = _engine(ports)
    session = _session()

    async def mixed_read(point_ids):
        return (
            ProtocolSample(point_id="p1", value=1.0, quality=Quality.GOOD),
            ProtocolSample(point_id="p1", value=None, quality=Quality.BAD),
        )

    session._protocol.read = mixed_read  # type: ignore[method-assign]
    await engine.collect(session, "g", ["s1"], "t1:dev1")
    assert ports.success == [("t1:dev1", True)]


async def test_collect_read_timeout_reports_failure():
    ports = _RecordingPorts()
    engine = _engine(ports, read_timeout=0.05)
    session = _session()

    async def slow_read(point_ids):
        await asyncio.sleep(5)
        return ()

    session._protocol.read = slow_read  # type: ignore[method-assign]
    await engine.collect(session, "g", ["s1"], "t1:dev1")
    assert len(ports.failure) == 1 and "read timeout" in ports.failure[0][1]
    assert ports.read_failure == ["dev1"]


async def test_process_requires_bound_sink_dispatch():
    engine = AcquisitionEngine()
    with pytest.raises(RuntimeError, match="Sink"):
        await engine.process([PointValue(device_id="d", point_id="p", value=1.0)], ["s1"])


async def test_observer_exception_isolated():
    ports = _RecordingPorts()
    engine = _engine(ports)
    seen: list[int] = []

    def bad_observer(values):
        raise ValueError("boom")

    engine.add_observer(bad_observer)
    engine.add_observer(lambda values: seen.append(len(values)))
    await engine.collect(_session(), "g", ["s1"], "t1:dev1")
    assert seen == [1]
    assert ports.success == [("t1:dev1", False)]
