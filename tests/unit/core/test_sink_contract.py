"""共享 SinkPort 与统一配置入口测试。"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from core.application.port.sink import ExclusiveOpenSinkPort, SinkPort
from core.application.protocol_contract import ConnectionHealth
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import _SinkSchema

class MemorySink:
    def __init__(self) -> None:
        self.data: list[PointValue] = []

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def write(self, batch: list[PointValue]) -> None:
        self.data.extend(batch)

    async def flush(self) -> None:
        return None

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=True)


@pytest.mark.asyncio
async def test_port_delivery() -> None:
    port: SinkPort = MemorySink()
    value = PointValue("WT001", "power", 10.5)
    await port.open()
    await port.write([value])
    await port.flush()
    assert isinstance(port, MemorySink)
    assert port.data == [value]
    await port.close()


def test_utc_and_immutable() -> None:
    value = PointValue("WT001", "power", 10, timestamp=datetime.now(UTC))
    assert value.timestamp.utcoffset().total_seconds() == 0
    with pytest.raises(FrozenInstanceError):
        value.value = 2  # type: ignore[misc]


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PointValue("WT001", "power", 10, timestamp=datetime(2026, 1, 1))


@pytest.mark.parametrize("kind,connection", [
    ("file", {"path": "./data.csv"}),
    ("modbus", {"port": 1502}),
    ("redis", {"database": 1}),
])
def test_config_types(kind: str, connection: dict) -> None:
    assert _SinkSchema.model_validate({
        "name": "sink", "type": kind, "connection": connection
    }).type == kind


def test_old_type_rejected() -> None:
    with pytest.raises(ValueError):
        _SinkSchema.model_validate({
            "name": "sink", "type": "kafka",
            "connection": {"bootstrap_servers": "localhost", "topic": "telemetry"}
        })


def test_exclusive_open_is_optional() -> None:
    assert not isinstance(MemorySink(), ExclusiveOpenSinkPort)
