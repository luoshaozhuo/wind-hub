"""ADSDriver：五态、总预算、迟到清理、Net ID 占用与本机 AMS 生命周期。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import core.infrastructure.protocol.ads.driver as driver_module
from core.application.errors import ConfigError, ProtocolError
from core.application.protocol_contract import ProtocolWrite
from core.application.settings import ADSLocalConfig
from core.domain import PointTable, PointTableId, Protocol
from core.infrastructure.protocol.ads.driver import ADSDriver, ADSState
from core.infrastructure.protocol.ads.session import _AdsSession
from tests.component.core.protocol.ads.conftest import FakePyadsModule
from tests.support.ads_fake import FakePyadsConnection, Gate, make_driver, make_point, patch_session


def _driver(**options: object) -> ADSDriver:
    points = {"p": make_point("p", index_group=0x4020, index_offset=0)}
    return make_driver(points, options=options)  # type: ignore[arg-type]


async def test_connect_opens_sets_timeout_reads_state_and_publishes_connected(
    ads_identity: ADSLocalConfig,
) -> None:
    driver = _driver()
    connection = patch_session(driver, FakePyadsConnection())
    await driver.connect()
    assert driver.state is ADSState.CONNECTED
    assert driver.is_open()
    assert [name for name, _ in connection.calls] == ["open", "set_timeout", "read_state"]
    assert connection.timeouts == [ads_identity.request_timeout_ms]
    assert driver_module._TARGET_OWNERS["192.0.2.10.1.1"] is driver
    await driver.close()
    assert driver.state is ADSState.DISCONNECTED
    assert "192.0.2.10.1.1" not in driver_module._TARGET_OWNERS


async def test_concurrent_connects_coalesce_to_single_attempt(ads_identity: ADSLocalConfig) -> None:
    driver = _driver()
    connection = FakePyadsConnection()
    patch_session(driver, connection)
    await asyncio.gather(driver.connect(), driver.connect())
    assert driver.state is ADSState.CONNECTED
    assert sum(1 for name, _ in connection.calls if name == "open") == 1
    await driver.close()


async def test_connect_failure_closes_session_and_releases_identity(
    ads_identity: ADSLocalConfig,
) -> None:
    driver = _driver()
    connection = FakePyadsConnection()
    connection.read_state = lambda: (0, 0)  # PLC 不在 RUN 态  # type: ignore[method-assign]
    patch_session(driver, connection)
    with pytest.raises(ProtocolError, match="RUN"):
        await driver.connect()
    assert driver.state is ADSState.DISCONNECTED
    assert connection.closed_calls == 1
    assert "192.0.2.10.1.1" not in driver_module._TARGET_OWNERS
    # 干净失败后允许重新建链。
    connection.read_state = lambda: (5, 0)  # type: ignore[method-assign]
    await driver.connect()
    assert driver.is_open()
    await driver.close()


async def test_confirmed_physical_open_failure_releases_identity_and_reconnects(
    ads_identity: ADSLocalConfig,
) -> None:
    # D2：pyads 明确返回 open 失败（内部已回滚本地端口）时，driver 收尾
    # 是确定收敛而非 uncertain：状态回 DISCONNECTED、释放 Net ID 占用、
    # 不调物理 close，且修复后可用新会话重新建链。
    driver = _driver()
    first = FakePyadsConnection()
    first.fail["open"] = OSError("router refused")
    patch_session(driver, first)
    with pytest.raises(OSError, match="router refused"):
        await driver.connect()
    assert driver.state is ADSState.DISCONNECTED
    assert first.closed_calls == 0
    assert "192.0.2.10.1.1" not in driver_module._TARGET_OWNERS
    second = FakePyadsConnection()
    patch_session(driver, second)
    await driver.connect()
    assert driver.is_open()
    assert second.opened is True
    await driver.close()


async def test_close_failure_holds_faulted_identity_and_blocks_replacement(
    ads_identity: ADSLocalConfig,
) -> None:
    driver = _driver()
    connection = FakePyadsConnection()
    connection.fail["close"] = OSError("transport gone")
    patch_session(driver, connection)
    await driver.connect()
    with pytest.raises(OSError):
        await driver.close()
    assert driver.state is ADSState.FAULTED
    assert driver_module._TARGET_OWNERS["192.0.2.10.1.1"] is driver
    with pytest.raises(ProtocolError, match="not safely available"):
        await driver.connect()
    replacement = _driver()
    other = FakePyadsConnection()
    patch_session(replacement, other)
    with pytest.raises(ProtocolError, match="already in use"):
        await replacement.connect()
    assert other.opened is False


async def test_connect_deadline_cleans_late_open_and_allows_reconnect(
    ads_identity: ADSLocalConfig,
) -> None:
    driver_module._LOCAL_IDENTITY = replace(ads_identity, connect_timeout_ms=100)
    driver = _driver()
    connection = FakePyadsConnection()
    gate = Gate()
    connection.block_before["open"] = gate
    patch_session(driver, connection)
    with pytest.raises(ProtocolError, match="deadline exceeded"):
        await asyncio.wait_for(driver.connect(), timeout=2)
    assert driver.state is ADSState.CONNECTING
    gate.open()
    # 迟到的物理 open 完成后必须自行关闭并回到 DISCONNECTED。
    deadline = asyncio.get_running_loop().time() + 3
    while driver.state is not ADSState.DISCONNECTED:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    assert connection.closed_calls == 1
    assert "192.0.2.10.1.1" not in driver_module._TARGET_OWNERS


async def test_route_add_over_total_budget_must_not_start_second_connect(
    ads_identity: ADSLocalConfig,
    fake_pyads: FakePyadsModule,
) -> None:
    driver_module._LOCAL_IDENTITY = replace(ads_identity, connect_timeout_ms=100)
    driver = _driver(route_auto_add=True)
    # 必须是“明确的物理 open 失败”才会进入路由注册分支。
    first = FakePyadsConnection()
    first.fail_once["open"] = OSError("no route")
    second = FakePyadsConnection()
    sessions = iter([first, second])
    driver._new_session = lambda: _AdsSession(lambda: next(sessions))  # type: ignore[method-assign]

    route_gate = Gate()

    def blocked_route(*args: object) -> bool:
        route_gate.wait()
        return True

    fake_pyads.add_route_to_plc = blocked_route  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="deadline exceeded"):
        await driver.connect()
    assert driver.state is ADSState.CONNECTING
    route_gate.open()
    deadline = asyncio.get_running_loop().time() + 3
    while driver.state is not ADSState.DISCONNECTED:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    # 总预算已过期：即使路由注册成功也不得发起第二次物理 connect。
    assert second.opened is False
    assert second.closed_calls == 0
    # 首次为明确 open 失败（pyads 自回滚），不再下发物理 close。
    assert first.closed_calls == 0


async def test_route_auto_add_retries_once_after_route_registration(
    ads_identity: ADSLocalConfig,
    fake_pyads: FakePyadsModule,
) -> None:
    driver = _driver(route_auto_add=True)
    first = FakePyadsConnection()
    first.fail_once["open"] = OSError("no route")
    second = FakePyadsConnection()
    sessions = iter([first, second])
    driver._new_session = lambda: _AdsSession(lambda: next(sessions))  # type: ignore[method-assign]
    await driver.connect()
    assert driver.is_open()
    assert len(fake_pyads.route_calls) == 1
    net_id, local_ip, host, user, password = fake_pyads.route_calls[0]
    assert (net_id, local_ip, host) == ("192.0.2.1.1.1", "192.0.2.1", "192.0.2.10")
    assert user == "Administrator"
    # pyads 已在 open() 失败时自行回滚：不得再下发物理 close。
    assert first.closed_calls == 0
    assert second.opened is True
    assert second.closed_calls == 0
    await driver.close()
    assert second.closed_calls == 1


async def test_second_driver_for_same_net_id_rejected_until_first_closes(
    ads_identity: ADSLocalConfig,
) -> None:
    first = _driver()
    patch_session(first, FakePyadsConnection())
    await first.connect()
    second = _driver()
    blocked_connection = FakePyadsConnection()
    patch_session(second, blocked_connection)
    with pytest.raises(ProtocolError, match="already in use"):
        await second.connect()
    await first.close()
    patch_session(second, FakePyadsConnection())
    await second.connect()
    assert second.is_open()
    assert blocked_connection.opened is False
    await second.close()


async def test_close_deadline_reports_faulted_while_cleanup_continues(
    ads_identity: ADSLocalConfig,
) -> None:
    driver_module._LOCAL_IDENTITY = replace(ads_identity, close_timeout_ms=100)
    driver = _driver()
    connection = FakePyadsConnection()
    gate = Gate()
    connection.block_before["close"] = gate
    patch_session(driver, connection)
    await driver.connect()
    with pytest.raises(ProtocolError, match="close deadline exceeded"):
        await driver.close()
    assert driver.state is ADSState.FAULTED
    gate.open()
    deadline = asyncio.get_running_loop().time() + 3
    while driver.state is not ADSState.DISCONNECTED:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    assert connection.closed_calls == 1


async def test_initialize_local_idempotent_mismatch_and_close_gate(
    fake_pyads: FakePyadsModule,
) -> None:
    identity = ADSLocalConfig("192.0.2.1.1.1", "192.0.2.1")
    await ADSDriver.initialize_local(identity)
    await ADSDriver.initialize_local(identity)  # 幂等：不重复开端口
    assert fake_pyads.opened_ports == 1
    with pytest.raises(ConfigError, match="identity mismatch"):
        await ADSDriver.initialize_local(ADSLocalConfig("192.0.2.9.1.1", "192.0.2.9"))
    # 有 Driver 占用目标时拒绝拆除本机 AMS。
    driver = _driver()
    patch_session(driver, FakePyadsConnection())
    await driver.connect()
    with pytest.raises(ProtocolError, match="still own target routes"):
        await ADSDriver.close_local()
    await driver.close()
    await ADSDriver.close_local()
    assert fake_pyads.closed_ports == 1
    assert driver_module._LOCAL_CLOSED is True


async def test_connect_after_local_close_is_refused(ads_identity: ADSLocalConfig) -> None:
    await ADSDriver.close_local()
    driver = _driver()
    patch_session(driver, FakePyadsConnection())
    with pytest.raises(ProtocolError, match="not available"):
        await driver.connect()


async def test_disconnected_io_fails_before_network_and_unknown_point_rejected(
    ads_identity: ADSLocalConfig,
) -> None:
    driver = _driver()
    patch_session(driver, FakePyadsConnection())
    with pytest.raises(ProtocolError, match="not connected"):
        await driver.read_many(("p",))
    with pytest.raises(ProtocolError, match="not connected"):
        await driver.write_one(ProtocolWrite("ghost", 1))
    await driver.connect()
    with pytest.raises(ProtocolError, match="Unknown ADS point"):
        await driver.read_one("ghost")
    await driver.close()


async def test_symbol_points_resolve_at_connect_and_table_update_rules(
    ads_identity: ADSLocalConfig,
) -> None:
    symbol_point = make_point("s", symbol="MAIN.speed")
    driver = make_driver({"s": symbol_point})
    connection = FakePyadsConnection()
    connection.symbols["MAIN.speed"] = (0x4021, 0x10)
    patch_session(driver, connection)
    assert driver._points["s"].address_resolved is False
    await driver.connect()
    resolved = driver._points["s"]
    assert (resolved.group, resolved.offset) == (0x4021, 0x10)
    # 连接中变更 symbol 映射必须显式拒绝（需要重连重解析）。
    changed = PointTable(
        PointTableId("ads_pt"),
        Protocol("ads"),
        {"s": make_point("s", symbol="MAIN.other")},
    )
    with pytest.raises(ConfigError, match="reconnect"):
        driver.update_point_table(changed)
    # 同构 symbol 集合的纯 index 点表热更新在连接中允许。
    index_only = PointTable(
        PointTableId("ads_pt"),
        Protocol("ads"),
        {"p2": make_point("p2", index_group=0x4022, index_offset=0)},
    )
    driver2 = make_driver({"p2": make_point("p2", index_group=0x4022, index_offset=0)})
    driver2.update_point_table(index_only)
    assert "p2" in driver2._points
    await driver.close()
