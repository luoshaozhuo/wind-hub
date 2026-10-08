"""CollectorSinkService 应用层边界测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.runtime import CollectorRuntime
from wind_hub_collector.application.service.sink import CollectorSinkService
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config import RuntimeConfig
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue

pytestmark = pytest.mark.asyncio


def _sink(healthy: bool = True, message: str | None = None) -> MagicMock:
    sink = MagicMock(spec=SinkPort)
    sink.health = MagicMock(return_value=HealthStatus(healthy=healthy, message=message))
    sink.open = AsyncMock()
    sink.close = AsyncMock()
    sink.write = AsyncMock()
    sink.flush = AsyncMock()
    return sink


def _runtime(sinks: dict[str, MagicMock]) -> CollectorRuntime:
    return CollectorRuntime(
        devices={},
        sinks=sinks,
        engine=AcquisitionEngine(),
        config=RuntimeConfig(),
        tasks={},
    )


async def test_list_sinks_reports_health_and_queue_depth() -> None:
    runtime = _runtime({"s1": _sink(True), "s2": _sink(False, "down")})
    await runtime.sink_runtime.dispatch(
        {"s1": [PointValue(device_id="d1", point_id="p1", value=1)]}
    )
    service = CollectorSinkService(runtime)

    items = await service.list_sinks()

    by_name = {item.name: item for item in items}
    assert set(by_name) == {"s1", "s2"}
    assert by_name["s1"].healthy is True
    assert by_name["s1"].queue_depth == 1
    assert by_name["s2"].healthy is False
    assert by_name["s2"].message == "down"
    assert by_name["s2"].queue_depth == 0


async def test_verify_sink_returns_real_health_and_queue_depth() -> None:
    runtime = _runtime({"s1": _sink(True, "ok")})
    service = CollectorSinkService(runtime)

    item = await service.verify_sink("s1")

    assert item.name == "s1"
    assert item.healthy is True
    assert item.message == "ok"
    assert item.queue_depth == 0


async def test_verify_sink_unknown_raises_key_error() -> None:
    service = CollectorSinkService(_runtime({}))

    with pytest.raises(KeyError):
        await service.verify_sink("missing")


async def test_write_test_sink_writes_diagnostic_point_then_flushes() -> None:
    sink = _sink(True)
    order: list[str] = []
    sink.write = AsyncMock(side_effect=lambda batch: order.append("write"))
    sink.flush = AsyncMock(side_effect=lambda: order.append("flush"))
    service = CollectorSinkService(_runtime({"s1": sink}))

    result = await service.write_test_sink("s1")

    assert result.success is True
    assert result.message == ""
    # write 必须先于 flush。
    assert order == ["write", "flush"]
    batch = sink.write.await_args.args[0]
    assert len(batch) == 1
    point = batch[0]
    assert point.device_id == "_diagnostic"
    assert point.point_id == "_write_test"
    assert point.value == 1


async def test_write_test_sink_failure_returns_message() -> None:
    sink = _sink(True)
    sink.write = AsyncMock(side_effect=RuntimeError("boom"))
    service = CollectorSinkService(_runtime({"s1": sink}))

    result = await service.write_test_sink("s1")

    assert result.success is False
    assert result.message == "boom"
    sink.flush.assert_not_awaited()


async def test_write_test_sink_unknown_raises_key_error() -> None:
    service = CollectorSinkService(_runtime({}))

    with pytest.raises(KeyError):
        await service.write_test_sink("missing")
