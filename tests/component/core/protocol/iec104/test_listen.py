"""本地监听：过滤、多监听者、未配置地址隔离、回调异常隔离、取消语义。"""

from __future__ import annotations

import asyncio

from hat.drivers.iec101 import common as iec101

from tests.support.iec104_hat_fake import (
    connected_driver,
    floating_data_msg,
    make_point,
)


async def test_listener_receives_only_configured_and_filtered_points() -> None:
    points = [make_point("p1", 100), make_point("p2", 200)]
    driver, conn = await connected_driver(points)
    got: list[str] = []

    async def on_sample(sample) -> None:
        got.append(sample.point_id)

    handle = await driver.listen(("p1",), on_sample)
    conn.deliver(
        [
            floating_data_msg(1, 100, 1.0),
            floating_data_msg(1, 200, 2.0),  # 过滤掉
            floating_data_msg(1, 999, 3.0),  # 未配置地址，绝不投递
        ]
    )
    await asyncio.sleep(0.05)
    assert got == ["p1"]
    await handle.close()
    await driver.close()


async def test_multiple_listeners_share_delivery() -> None:
    driver, conn = await connected_driver([make_point("p1", 100)])
    a: list[float] = []
    b: list[float] = []
    ha = await driver.listen((), lambda s: _append(a, s.value))
    hb = await driver.listen((), lambda s: _append(b, s.value))
    conn.deliver([floating_data_msg(1, 100, 4.0)])
    await asyncio.sleep(0.05)
    assert a == [4.0] and b == [4.0]
    await ha.close()
    conn.deliver([floating_data_msg(1, 100, 5.0)])
    await asyncio.sleep(0.05)
    assert a == [4.0] and b == [4.0, 5.0]
    await hb.close()
    await driver.close()


async def _append(target: list[float], value: object) -> None:
    target.append(float(value))  # type: ignore[arg-type]


async def test_throwing_callback_does_not_kill_delivery() -> None:
    driver, conn = await connected_driver([make_point("p1", 100), make_point("p2", 200)])
    received: list[str] = []

    async def boom(_sample) -> None:
        raise RuntimeError("listener boom")

    async def collect(sample) -> None:
        received.append(sample.point_id)

    await driver.listen(("p1",), boom)
    await driver.listen(("p1", "p2"), collect)
    conn.deliver(
        [floating_data_msg(1, 100, 1.0), floating_data_msg(1, 200, 2.0)]
    )
    await asyncio.sleep(0.05)
    assert received == ["p1", "p2"]
    await driver.close()


async def test_listen_close_delivers_no_further_samples_and_is_idempotent() -> None:
    driver, conn = await connected_driver([make_point("p1", 100)])
    received: list[float] = []

    async def on_sample(sample) -> None:
        await asyncio.sleep(0.02)
        received.append(sample.value)

    handle = await driver.listen((), on_sample)
    conn.deliver([floating_data_msg(1, 100, 1.0)])
    await asyncio.sleep(0.05)
    await handle.close()
    await handle.close()
    conn.deliver([floating_data_msg(1, 100, 2.0)])
    await asyncio.sleep(0.05)
    assert received == [1.0]
    await driver.close()


async def test_interrogated_and_remote_command_data_also_delivered() -> None:
    driver, conn = await connected_driver([make_point("p1", 100)])
    causes: list[object] = []

    async def on_sample(sample) -> None:
        causes.append(sample.point_id)

    await driver.listen((), on_sample)
    conn.deliver(
        [
            floating_data_msg(1, 100, 1.0, cause=iec101.DataResCause.INTERROGATED_STATION),
            floating_data_msg(1, 100, 2.0, cause=iec101.DataResCause.REMOTE_COMMAND),
        ]
    )
    await asyncio.sleep(0.05)
    assert causes == ["p1", "p1"]
    await driver.close()
