"""R4-G G-04/QUAL：Collector 传输确认样本证据的有界保留契约。

全局尾部窗口会在宽点位扇出下把 Sink 订阅点位的证据挤出，导致跨服务 pipeline
关联永久不可证。证据缓存必须按（设备、点位）保留最新一条且总量有界。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from collector.infrastructure.adapters.outbound.publisher import CollectorPublisher
from core.application import ProtocolSample
from core.domain import (
    BusinessPoint,
    DataType,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    Quantity,
    Unit,
)

pytestmark = pytest.mark.unit


class _FakeConnection:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.is_connected = True

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def drain(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _publisher() -> CollectorPublisher:
    return CollectorPublisher(
        servers=["nats://127.0.0.1:4222"],
        site_id="farm-a",
        collector_id="collector-rt",
        delivery="realtime",
    )


def _sample(point_id: str) -> ProtocolSample:
    return ProtocolSample(point_id=point_id, value=1.0, timestamp=datetime.now(UTC))


def _pack(publisher: CollectorPublisher, device_id: str, samples: tuple[ProtocolSample, ...]):
    unit = Unit("kilowatt", "kW", Quantity("active_power"))
    business = BusinessPoint("active_power", DataType.FLOAT64, unit)
    table = PointTable(
        "modbus_wtg@1.0.0",
        Protocol("modbus"),
        {
            sample.point_id: Point(sample.point_id, "active_power", unit, PointAccess.READ)
            for sample in samples
        },
    )
    return publisher._builder.pack(
        device_id,
        samples,
        device_type="turbine",
        point_table_id="point_table_modbus_wtg_v1.0.0",
        point_table=table,
        business_points={"active_power": business},
    )


async def test_evidence_keeps_latest_sample_per_device_and_point() -> None:
    publisher = _publisher()
    publisher._connection = _FakeConnection()

    await publisher._send(_pack(publisher, "WTG_SIM01", (_sample("wind_speed"),))[0])
    first = next(
        item for item in publisher.last_sample_evidence if item["point_id"] == "wind_speed"
    )

    # 宽扇出：大量其他点位的批次进入缓存。
    await publisher._send(
        _pack(publisher, "WTG_SIM01", tuple(_sample(f"other_point_{i}") for i in range(40)))[0]
    )
    await publisher._send(
        _pack(publisher, "PV_SIM01", tuple(_sample(f"pv_{i}") for i in range(40)))[0]
    )

    evidence = publisher.last_sample_evidence
    # 被大量其他点位冲刷后，wind_speed 的确认证据仍然可用于关联。
    wind = [item for item in evidence if item["point_id"] == "wind_speed"]
    assert len(wind) == 1
    assert wind[0]["device_id"] == "WTG_SIM01"
    assert wind[0]["sample_id"] == first["sample_id"]

    # 同点位刷新后保留有界历史（K 条），新条目在尾部。
    for _ in range(CollectorPublisher._EVIDENCE_HISTORY + 3):
        await publisher._send(_pack(publisher, "WTG_SIM01", (_sample("wind_speed"),))[0])
    wind = [
        item
        for item in publisher.last_sample_evidence
        if item["point_id"] == "wind_speed" and item["device_id"] == "WTG_SIM01"
    ]
    assert len(wind) == CollectorPublisher._EVIDENCE_HISTORY
    assert wind[-1]["sample_id"] != first["sample_id"]
    assert first["sample_id"] not in {item["sample_id"] for item in wind}


async def test_evidence_is_bounded_and_diagnostics_filter_by_device() -> None:
    publisher = _publisher()
    publisher._connection = _FakeConnection()

    for device in ("WTG_SIM01", "PV_SIM01", "BESS_SIM01"):
        for index in range(120):
            await publisher._send(_pack(publisher, device, (_sample(f"point_{index}"),))[0])

    evidence = publisher.last_sample_evidence
    # 键数有界；每个（设备、点位）的历史不超过 K。
    distinct_keys = {(item["device_id"], item["point_id"]) for item in evidence}
    assert len(distinct_keys) <= CollectorPublisher._EVIDENCE_MAX_KEYS
    assert all(item["sample_id"] and item["confirmed_at"] for item in evidence)
    for key in distinct_keys:
        assert (
            sum((item["device_id"], item["point_id"]) == key for item in evidence)
            <= CollectorPublisher._EVIDENCE_HISTORY
        )

    # 诊断侧按设备过滤（与 CollectorDiagnostics NATS 阶段同一谓词）。
    wtg = [item for item in evidence if item.get("device_id") == "WTG_SIM01"]
    assert wtg and all(item["device_id"] == "WTG_SIM01" for item in wtg)


async def test_evidence_starts_empty_and_ignores_unrelated_devices() -> None:
    publisher = _publisher()
    publisher._connection = _FakeConnection()
    assert publisher.last_sample_evidence == ()

    await publisher._send(_pack(publisher, "OTHER_DEVICE", (_sample("wind_speed"),))[0])
    filtered = [
        item for item in publisher.last_sample_evidence if item.get("device_id") == "WTG_SIM01"
    ]
    assert filtered == []
