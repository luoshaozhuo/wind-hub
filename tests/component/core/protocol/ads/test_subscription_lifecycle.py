"""Subscription start/stop/driver.close 竞态与 Handle 归属（P0-4）。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import core.infrastructure.protocol.ads.driver as driver_module
from core.application.protocol_contract import ProtocolSample
from core.application.settings import ADSLocalConfig
from tests.support.ads_fake import FakePyadsConnection, Gate, make_driver, make_point, patch_session


def _connected_driver(connection: FakePyadsConnection, points: dict | None = None) -> object:
    points = points or {
        "a": make_point("a", data_type="INT", index_group=0x4020, index_offset=0),
        "b": make_point("b", data_type="INT", index_group=0x4020, index_offset=2),
    }
    driver = make_driver(points)
    patch_session(driver, connection)
    return driver


async def _noop(batch: tuple[ProtocolSample, ...]) -> None:
    return None


async def test_start_registers_and_stop_deletes_once_idempotent(
    ads_identity: ADSLocalConfig,
) -> None:
    connection = FakePyadsConnection()
    driver = _connected_driver(connection)
    await driver.connect()
    sub = driver.subscribe(("a", "b"), _noop, interval=0.05)
    await sub.start()
    assert len(connection.notifications) == 1
    await sub.stop()
    adds = [name for name, _ in connection.calls if name == "add_device_notification"]
    dels = [args for name, args in connection.calls if name == "del_device_notification"]
    assert adds == ["add_device_notification"]
    assert len(dels) == 1
    assert connection.notifications == {}
    # 重复 stop 不重复删除。
    await sub.stop()
    assert [name for name, _ in connection.calls if name == "del_device_notification"] == [
        "del_device_notification"
    ]
    await driver.close()


async def test_start_is_single_shot(ads_identity: ADSLocalConfig) -> None:
    connection = FakePyadsConnection()
    driver = _connected_driver(connection)
    await driver.connect()
    sub = driver.subscribe(("a",), _noop)
    await sub.start()
    with pytest.raises(RuntimeError, match="start once"):
        await sub.start()
    await sub.stop()
    await driver.close()


async def test_cancelled_start_with_late_success_still_deletes_handle(
    ads_identity: ADSLocalConfig,
) -> None:
    connection = FakePyadsConnection()
    gate = Gate()
    connection.block_before["add_device_notification"] = gate
    driver = _connected_driver(connection)
    await driver.connect()
    sub = driver.subscribe(("a",), _noop)
    start = asyncio.create_task(sub.start())
    await asyncio.sleep(0.05)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert gate.waiters == 1
    gate.open()
    # 迟到的注册可能成功：stop 必须收敛并删除该 Handle。
    await sub.stop()
    assert connection.notifications == {}
    assert len([n for n, _ in connection.calls if n == "del_device_notification"]) == 1
    await driver.close()


async def test_unconfirmed_registration_reports_uncertain(ads_identity: ADSLocalConfig) -> None:
    connection = FakePyadsConnection()
    connection.fail["add_device_notification"] = RuntimeError("ams router ambiguity")
    driver = _connected_driver(connection)
    await driver.connect()
    sub = driver.subscribe(("a",), _noop)
    with pytest.raises(RuntimeError, match="ams router ambiguity"):
        await sub.start()
    with pytest.raises(ExceptionGroup, match="cleanup failed") as exc_info:
        await sub.stop()
    assert any("unconfirmed" in str(exc) for exc in exc_info.value.exceptions)
    # 不确定状态不能被 driver.close 吞掉：FAULTED 保留 Net ID 占用。
    with pytest.raises(ExceptionGroup):
        await driver.close()
    assert driver.state.value == "faulted"


async def test_failed_delete_retains_handle_and_stays_fail_closed(
    ads_identity: ADSLocalConfig,
) -> None:
    # D4：注销失败时原生 Handle 不得从簿记中丢失：token 保留、订阅不脱离
    # driver 注册表、后续 stop/driver.close 持续显式上报并 FAULTED
    # （物理路由最终随 session close 拆除，但本地绝不谎报已收敛）。
    connection = FakePyadsConnection()
    connection.fail["del_device_notification"] = OSError("router gone")
    driver = _connected_driver(connection)
    await driver.connect()
    sub = driver.subscribe(("a",), _noop)
    await sub.start()
    token = sub._handles[0]
    with pytest.raises(ExceptionGroup):
        await sub.stop()
    assert sub._handles == [token]  # Handle 未被丢弃
    assert connection.notifications != {}
    assert sub in driver._subscriptions
    with pytest.raises(ExceptionGroup):
        await sub.stop()  # 不允许静默成功
    with pytest.raises(ExceptionGroup):
        await driver.close()
    assert driver.state.value == "faulted"


async def test_successful_stop_unregisters_subscription_no_churn_accumulation(
    ads_identity: ADSLocalConfig,
) -> None:
    # D5：stop 成功后订阅必须从 driver 注册表解除；高频 start/stop churn
    # 不累积 Subscription 对象，原生 add/del 始终配对。
    connection = FakePyadsConnection()
    driver = _connected_driver(connection)
    await driver.connect()
    for _ in range(50):
        sub = driver.subscribe(("a",), _noop, interval=0.05)
        await sub.start()
        await sub.stop()
    assert driver._subscriptions == set()
    adds = [n for n, _ in connection.calls if n == "add_device_notification"]
    dels = [n for n, _ in connection.calls if n == "del_device_notification"]
    assert len(adds) == 50
    assert len(dels) == 50
    assert connection.notifications == {}
    await driver.close()


async def test_delete_failure_makes_driver_close_faulted(ads_identity: ADSLocalConfig) -> None:
    connection = FakePyadsConnection()
    connection.fail["del_device_notification"] = OSError("router gone")
    driver = _connected_driver(connection)
    await driver.connect()
    sub = driver.subscribe(("a",), _noop)
    await sub.start()
    with pytest.raises(ExceptionGroup):
        await sub.stop()
    with pytest.raises(ExceptionGroup):
        await driver.close()
    assert driver.state.value == "faulted"
    assert driver_module._TARGET_OWNERS["192.0.2.10.1.1"] is driver


async def test_driver_close_stops_active_subscription_and_settles(
    ads_identity: ADSLocalConfig,
) -> None:
    connection = FakePyadsConnection()
    driver = _connected_driver(connection)
    await driver.connect()
    sub = driver.subscribe(("a",), _noop)
    await sub.start()
    await driver.close()
    assert driver.state.value == "disconnected"
    assert len([n for n, _ in connection.calls if n == "del_device_notification"]) == 1
    assert connection.closed_calls == 1


async def test_close_budget_expires_when_worker_callback_still_alive(
    ads_identity: ADSLocalConfig,
) -> None:
    driver_module._LOCAL_IDENTITY = replace(ads_identity, close_timeout_ms=200)
    connection = FakePyadsConnection()
    driver = _connected_driver(connection)
    await driver.connect()
    release = asyncio.Event()

    async def stuck(batch: tuple[ProtocolSample, ...]) -> None:
        # 模拟拒绝在清理窗口内结束的回调（吞掉取消、持续存活）。
        try:
            await release.wait()
        except asyncio.CancelledError:
            while not release.is_set():
                await asyncio.sleep(0.01)
            raise

    sub = driver.subscribe(("a",), stuck)
    await sub.start()
    connection.fire_notification((0x4020, 0), b"\x2a\x00")
    await asyncio.sleep(0.05)  # worker 已进入不可及时取消的回调
    with pytest.raises(Exception, match="close deadline exceeded"):
        await driver.close()
    assert driver.state.value == "faulted"
    release.set()
    # 后台清理在回调结束后收敛。
    deadline = asyncio.get_running_loop().time() + 3
    while driver.state is not driver_module.ADSState.DISCONNECTED:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
