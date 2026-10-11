from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol

import pytest

from core.application import (
    ConfigError,
    ProtocolCapability,
    ProtocolCapabilityError,
    ProtocolError,
    ProtocolSample,
)
from core.domain import (
    UNIT_CATALOG,
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    UnitCode,
)
from core.domain import (
    Protocol as DeviceProtocol,
)
from core.infrastructure.protocol.iec104 import IEC104Driver


class _Closable(Protocol):
    async def close(self) -> None: ...


def _endpoint() -> ConnectionEndpoint:
    return ConnectionEndpoint("192.0.2.30")


def _point(
    point_id: str,
    *,
    access: PointAccess = PointAccess.READ,
    ext: dict[str, str | int] | None = None,
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=access,
        ext={} if ext is None else ext,
    )


def _point_ext(
    *,
    ioa: int,
    type_id: str | None = None,
) -> dict[str, str | int]:
    options: dict[str, str | int] = {"ioa": ioa}
    if type_id is not None:
        options["type_id"] = type_id
    return options


def _driver_for_monitoring_point() -> tuple[IEC104Driver, Point]:
    point = _point("power", ext=_point_ext(ioa=100))
    table = PointTable(
        PointTableId("iec_pt"),
        DeviceProtocol("iec104"),
        {point.point_id: point},
    )
    driver = IEC104Driver(
        _endpoint(),
        table,
        {},
    )
    return driver, point


@pytest.mark.asyncio
async def test_iec104_subscription_close_drains_inflight_callback() -> None:
    driver, point = _driver_for_monitoring_point()
    started = asyncio.Event()
    release = asyncio.Event()
    received: list[float | int | bool | str | None] = []

    async def callback(sample: ProtocolSample) -> None:
        started.set()
        await release.wait()
        received.append(sample.value)

    handle = await driver.subscribe((point.point_id,), callback)
    driver._closed = False
    driver._is_open = True
    driver._store_sample(
        100,
        ProtocolSample(
            point_id="power",
            value=42.0,
            timestamp=datetime.now(UTC),
        ),
    )
    await started.wait()

    closer = asyncio.create_task(handle.close())
    await asyncio.sleep(0)
    assert not closer.done()

    release.set()
    await closer
    assert received == [42.0]

    driver._store_sample(
        100,
        ProtocolSample(
            point_id="power",
            value=43.0,
            timestamp=datetime.now(UTC),
        ),
    )
    await asyncio.sleep(0)
    assert received == [42.0]


@pytest.mark.asyncio
async def test_iec104_subscription_can_close_itself_from_callback() -> None:
    driver, point = _driver_for_monitoring_point()
    done = asyncio.Event()
    handle_box: list[_Closable] = []

    async def callback(sample: ProtocolSample) -> None:
        del sample
        await handle_box[0].close()
        done.set()

    handle = await driver.subscribe((point.point_id,), callback)
    handle_box.append(handle)
    driver._closed = False
    driver._is_open = True
    driver._store_sample(
        100,
        ProtocolSample(
            point_id="power",
            value=1.0,
            timestamp=datetime.now(UTC),
        ),
    )

    await asyncio.wait_for(done.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_iec104_point_table_update_changes_read_addressing() -> None:
    driver, _ = _driver_for_monitoring_point()
    driver._is_open = True
    replacement = _point("wind_speed", ext=_point_ext(ioa=100))
    driver.update_point_table(
        PointTable(PointTableId("iec_pt"), DeviceProtocol("iec104"), {"wind_speed": replacement})
    )
    assert "power" not in driver._points_by_id
    assert driver._points_by_id["wind_speed"].ioa == 100
    with pytest.raises(ConfigError, match="power"):
        await driver.read_one("power")
    with pytest.raises(ProtocolError, match="active read"):
        await driver.read_one("wind_speed")


def test_iec104_invalid_point_table_update_is_atomic() -> None:
    driver, _ = _driver_for_monitoring_point()
    invalid = _point("control", access=PointAccess.WRITE, ext=_point_ext(ioa=200))
    with pytest.raises(ConfigError, match="requires type_id"):
        driver.update_point_table(
            PointTable(PointTableId("invalid"), DeviceProtocol("iec104"), {"control": invalid})
        )
    assert driver._point_table_id == "iec_pt"
    assert list(driver._points_by_id) == ["power"]
    assert driver._points_by_ioa[100].point_id == "power"


@pytest.mark.asyncio
async def test_iec104_queued_old_mapping_sample_is_ignored() -> None:
    driver, _ = _driver_for_monitoring_point()
    driver._closed = False
    driver._is_open = True
    received: list[ProtocolSample] = []

    async def callback(sample: ProtocolSample) -> None:
        received.append(sample)

    handle = await driver.subscribe(("power",), callback)
    replacement = _point("wind_speed", ext=_point_ext(ioa=100))
    driver.update_point_table(
        PointTable(PointTableId("iec_pt"), DeviceProtocol("iec104"), {"wind_speed": replacement})
    )
    driver._store_sample(
        100,
        ProtocolSample(point_id="power", value=42.0, timestamp=datetime.now(UTC)),
    )
    await asyncio.sleep(0)
    await handle.close()
    assert received == []


@pytest.mark.asyncio
async def test_iec104_rejects_batch_operations_without_network_io() -> None:
    driver, _ = _driver_for_monitoring_point()
    assert ProtocolCapability.WRITE_MANY not in driver.capabilities()
    with pytest.raises(ProtocolCapabilityError, match="read_many"):
        await driver.read_many(("power",))
    with pytest.raises(ProtocolCapabilityError, match="write_many"):
        await driver.write_many(())


@pytest.mark.asyncio
async def test_slow_subscriber_coalesces_new_samples_per_point() -> None:
    """慢订阅者期间，同一点的待派发样本按点合并：中间值丢弃、最新值至多派发一次。"""
    driver, _ = _driver_for_monitoring_point()
    driver._closed = False
    driver._is_open = True
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[object] = []
    first = True

    async def callback(sample: ProtocolSample) -> None:
        nonlocal first
        seen.append(sample.value)
        if first:
            first = False
            started.set()
            await release.wait()

    handle = await driver.subscribe(("power",), callback)
    driver._store_sample(
        100, ProtocolSample(point_id="power", value=1, timestamp=datetime.now(UTC))
    )
    await asyncio.wait_for(started.wait(), 1)
    for value in range(2, 20):
        driver._store_sample(
            100, ProtocolSample(point_id="power", value=value, timestamp=datetime.now(UTC))
        )
    assert seen == [1]

    release.set()
    # 合并后的最新值 19 至多派发一次；中间值 2..18 不得出现。
    deadline = asyncio.get_running_loop().time() + 1.0
    while len(seen) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    assert seen == [1, 19]

    await asyncio.wait_for(handle.close(), 1)
    driver._store_sample(
        100, ProtocolSample(point_id="power", value=20, timestamp=datetime.now(UTC))
    )
    await asyncio.sleep(0)
    assert seen == [1, 19]


@pytest.mark.asyncio
async def test_negative_unsolicited_message_is_not_dispatched() -> None:
    driver, _ = _driver_for_monitoring_point()
    driver._closed = False
    driver._is_open = True
    seen: list[ProtocolSample] = []

    async def callback(sample: ProtocolSample) -> None:
        seen.append(sample)

    handle = await driver.subscribe(("power",), callback)
    driver._store_sample(
        100,
        ProtocolSample(point_id="power", value=1, timestamp=datetime.now(UTC)),
        negative=True,
    )
    await asyncio.sleep(0)
    await handle.close()
    assert seen == []


@pytest.mark.asyncio
async def test_duplicate_subscription_point_does_not_duplicate_delivery() -> None:
    driver, _ = _driver_for_monitoring_point()
    driver._closed = False
    driver._is_open = True
    seen = []
    received = asyncio.Event()

    async def callback(sample: ProtocolSample) -> None:
        seen.append(sample.value)
        received.set()

    handle = await driver.subscribe(("power", "power"), callback)
    driver._store_sample(
        100, ProtocolSample(point_id="power", value=7, timestamp=datetime.now(UTC))
    )
    await asyncio.wait_for(received.wait(), 1)
    await handle.close()
    assert seen == [7]
