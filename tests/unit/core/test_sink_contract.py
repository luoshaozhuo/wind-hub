"""共享 Sink 契约与 PointValue 模型的独立单元测试。"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from core.application.port.sink import ExclusiveOpenSinkPort, SinkPort
from core.application.protocol_contract import ConnectionHealth, Quality
from core.domain.point_value import PointValue


def _point() -> PointValue:
    return PointValue(
        device_id="WT001",
        point_id="wind_speed",
        value=8.5,
        timestamp=datetime(2026, 10, 10, tzinfo=UTC),
    )


def test_point_value_is_immutable_and_utc() -> None:
    point = _point()
    assert point.quality is Quality.GOOD
    assert point.timestamp.utcoffset() == timedelta(0)
    with pytest.raises(FrozenInstanceError):
        point.value = 9.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("device_id", "point_id"),
    [("", "p"), ("wt", " "), (" ", "p")],
)
def test_point_value_rejects_empty_identifiers(device_id: str, point_id: str) -> None:
    with pytest.raises(ValueError):
        PointValue(
            device_id=device_id,
            point_id=point_id,
            value=1,
            timestamp=datetime.now(UTC),
        )


def test_point_value_rejects_naive_and_non_utc_timestamps() -> None:
    with pytest.raises(ValueError):
        PointValue(device_id="wt", point_id="p", value=1, timestamp=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        PointValue(
            device_id="wt",
            point_id="p",
            value=1,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))),
        )


class _Sink:
    exclusive_open = True

    def __init__(self) -> None:
        self.items: list[PointValue] = []

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def write(self, batch: list[PointValue]) -> None:
        self.items.extend(batch)

    async def flush(self) -> None:
        pass

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=True)


@pytest.mark.asyncio
async def test_sink_port_is_implementable() -> None:
    sink: SinkPort = _Sink()
    assert isinstance(sink, ExclusiveOpenSinkPort)
    await sink.open()
    await sink.write([_point()])
    await sink.flush()
    assert len(sink.items) == 1
    assert sink.health().healthy
    await sink.close()
