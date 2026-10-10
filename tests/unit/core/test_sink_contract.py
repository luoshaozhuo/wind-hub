"""新增 Core Sink 契约测试，不依赖任何具体 Adapter。"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from core.application.port.sink import ExclusiveOpenSinkPort, SinkPort
from core.application.protocol_contract import ConnectionHealth
from core.application.sink_contract import SinkConfig
from core.domain.point_value import PointValue


class MemorySink:
    def __init__(self) -> None:
        self.data: list[PointValue] = []

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def write(self, batch: list[PointValue]) -> None:
        self.data.extend(batch)

    async def flush(self) -> None:
        pass

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=True)


@pytest.mark.asyncio
async def test_sink_port_supports_batch_write() -> None:
    sink: SinkPort = MemorySink()
    value = PointValue(device_id="WT001", point_id="power", value=100.0)
    await sink.open()
    await sink.write([value])
    await sink.flush()
    assert isinstance(sink, MemorySink)
    assert sink.data == [value]
    assert sink.health().healthy
    await sink.close()


def test_point_value_is_utc_and_immutable() -> None:
    local = datetime(2026, 10, 10, 12, tzinfo=UTC) + timedelta(hours=0)
    value = PointValue(device_id="WT001", point_id="power", value=1, timestamp=local)
    assert value.timestamp.utcoffset() == timedelta(0)
    with pytest.raises(FrozenInstanceError):
        value.value = 2  # type: ignore[misc]


def test_point_value_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PointValue(device_id="WT001", point_id="power", value=1, timestamp=datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("kind", "connection"),
    [
        ("file", {"path": "./data"}),
        ("modbus", {"port": 1502}),
        ("redis", {"database": 1}),
    ],
)
def test_sink_config_accepts_only_three_types(kind: str, connection: dict[str, object]) -> None:
    config = TypeAdapter(SinkConfig).validate_python(
        {"name": "output", "type": kind, "connection": connection}
    )
    assert config.type == kind


def test_sink_config_rejects_old_types() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(SinkConfig).validate_python(
            {"name": "output", "type": "kafka", "connection": {}}
        )


def test_file_sink_config_rejects_empty_path() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(SinkConfig).validate_python(
            {"name": "output", "type": "file", "connection": {"path": "  "}}
        )


def test_exclusive_open_is_optional() -> None:
    assert not isinstance(MemorySink(), ExclusiveOpenSinkPort)
