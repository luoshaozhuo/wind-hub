"""ST-20 / ST-55 / ST-56 ADS 点表热更新与 Notification 句柄收敛。

对端为 pyads AdsTestServer 经 scripts.protocol_servers._ADSVirtualServer 封装的
真实 AMS/TCP 回环栈（非 TwinCAT 硬件；硬件验收另记 NOT_RUN）。

ST-20：Symbol/Notification 热更新不重建 Session，增量核对符号，
       新代次句柄一致可核对，旧句柄注销清理。
ST-55：提交后旧句柄注销失败——applied=True 与新 active_version 保留，
       资源状态 FAULTED、残留资源完整可观察，不假回滚。
ST-56：热更新与 Notification 持续上送并发，代次门阻断重复/跨代投递，
       更新后订阅继续工作且停止时句柄收敛。
另含 ST-54 的 ADS 预备期故障与超时证据（映射不切换、代次不递增）。
"""

from __future__ import annotations

import asyncio
import random

import pytest

from core.application.protocol_contract import PointTableUpdateStatus
from core.application.settings import ADSLocalConfig
from core.domain import ConnectionEndpoint, PointAccess
from core.domain.value_objects import DataType
from core.infrastructure.protocol.ads import ADSDriver
from scripts.protocol_servers import EndpointPlan, _ADSVirtualServer
from tests.core.protocol.conftest import make_point, make_table

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.ads,
    pytest.mark.concurrency,
]


def _plan(host_ip: str, device_id: str, points: tuple) -> EndpointPlan:
    return EndpointPlan(
        device_id=device_id,
        protocol="ads",
        host=host_ip,
        port=48898,
        options={},
        points=points,
        data_types={point.point_id: DataType.FLOAT32 for point in points},
    )


def _driver(host_ip: str, table) -> ADSDriver:
    return ADSDriver(
        ConnectionEndpoint(host_ip, 851),
        table,
        {"target_net_id": f"{host_ip}.1.1"},
    )


async def _stack(host_ip: str, device_id: str, server_points: tuple, driver_table):
    server = _ADSVirtualServer(_plan(host_ip, device_id, server_points))
    driver = _driver(host_ip, driver_table)
    await server.start()
    await ADSDriver.initialize_local(ADSLocalConfig("127.0.0.1.1.1", "127.0.0.1"))
    await driver.connect()
    return server, driver


async def _teardown(server: _ADSVirtualServer, driver: ADSDriver) -> None:
    try:
        await driver.close()
    finally:
        await ADSDriver.close_local()
        await server.stop()


def _power() -> object:
    return make_point("power", access=PointAccess.READ, symbol="MAIN.power", data_type="REAL")


def _speed() -> object:
    return make_point("speed", access=PointAccess.READ, symbol="MAIN.speed", data_type="REAL")


def _power_with_resolved_address(driver: ADSDriver) -> object:
    """同符号、同地址的显式 index 配置：与未解析版本不同，使订阅进入受影响集。"""
    resolved = driver._points["power"]
    return make_point(
        "power",
        access=PointAccess.READ,
        symbol="MAIN.power",
        data_type="REAL",
        index_group=resolved.group,
        index_offset=resolved.offset,
    )


async def test_st_20_hot_update_keeps_session_and_converges_handles() -> None:
    host_ip = "127.0.0.39"
    server, driver = await _stack(
        host_ip, "ads-st20", (_power(), _speed()), make_table("ads", _power())
    )
    try:
        session_before = driver._session
        # connect 成功即建立首代点表，active_version=1。
        assert driver._active_version == 1

        received = asyncio.Event()
        values: list = []

        async def callback(batch: tuple) -> None:
            values.extend(batch)
            received.set()

        subscription = driver.subscribe(["power"], callback, interval=0.05)
        await subscription.start()
        try:
            server.update(random.Random(20))
            await asyncio.wait_for(received.wait(), timeout=3.0)
            assert values and values[-1].point_id == "power"
            handles_before = list(subscription._handles)
            assert handles_before

            result = await driver.update_point_table(
                make_table("ads", _power_with_resolved_address(driver), _speed()),
                timeout=5.0,
            )
            assert result.status is PointTableUpdateStatus.SUCCESS
            assert result.applied is True
            assert result.active_version == 2
            assert result.resource_state == "READY"
            # Session 未重建：同一原生连接对象承载新映射。
            assert driver._session is session_before
            # 新增符号经真实 get_symbol 增量核对后可读。
            sample = await driver.read_one("speed")
            assert sample.point_id == "speed"
            # 订阅受影响并完成代次切换：新句柄生效，旧句柄注销清理无残留。
            assert subscription._handles
            assert subscription._handles != handles_before
            assert subscription._retired_handles == []
            assert subscription._prepared_handles == []
        finally:
            await subscription.stop()
        assert subscription._handles == []
    finally:
        await _teardown(server, driver)


async def test_st_55_post_commit_cleanup_failure_keeps_new_map_and_reports_residual() -> None:
    host_ip = "127.0.0.40"
    server, driver = await _stack(
        host_ip, "ads-st55", (_power(), _speed()), make_table("ads", _power())
    )
    try:
        subscription = driver.subscribe(["power"], lambda batch: None, interval=0.5)
        await subscription.start()
        original_table = driver._point_table

        async def leak_cleanup() -> tuple[str, ...]:
            # 模拟旧句柄 native 注销未确认：残留描述必须原样上报。
            return ("ADS subscription:0:notification-handle-not-released",)

        subscription.cleanup_retired = leak_cleanup  # type: ignore[method-assign]

        result = await driver.update_point_table(
            make_table("ads", _power_with_resolved_address(driver), _speed()),
            timeout=5.0,
        )
        # 提交已完成：新映射与代次保留，禁止伪回滚。
        assert result.applied is True
        assert result.active_version == 2
        assert result.status is PointTableUpdateStatus.APPLY_FAILED
        assert result.resource_state == "FAULTED"
        assert result.residual_resources == ("ADS subscription:0:notification-handle-not-released",)
        assert driver._point_table is not original_table
        assert "speed" in driver._points
        await subscription.stop()
    finally:
        await _teardown(server, driver)


async def test_st_54_prepare_failure_and_timeout_never_switch_mapping() -> None:
    host_ip = "127.0.0.41"
    server, driver = await _stack(
        host_ip, "ads-st54", (_power(), _speed()), make_table("ads", _power())
    )
    try:
        subscription = driver.subscribe(["power"], lambda batch: None, interval=0.5)
        await subscription.start()
        original_table = driver._point_table

        async def explode_prepare(_points: object) -> None:
            raise RuntimeError("native registration rejected")

        subscription.prepare_update = explode_prepare  # type: ignore[method-assign]
        prepare_failed = await driver.update_point_table(
            make_table("ads", _power_with_resolved_address(driver), _speed()),
            timeout=5.0,
        )
        assert prepare_failed.status is PointTableUpdateStatus.APPLY_FAILED
        assert prepare_failed.applied is False
        assert prepare_failed.active_version == 1
        assert driver._point_table is original_table
        assert "speed" not in driver._points

        # 超时路径：符号核对永久悬挂，必须在截止时间返回 TIMEOUT 且不切换映射。
        # 仅阻塞 get_symbol；句柄注销等操作透传真实 session，保证 teardown 可收敛。
        real_execute = driver._session.execute

        async def hang_execute(operation: str, **kwargs: object) -> object:
            if operation == "get_symbol":
                await asyncio.Event().wait()
            return await real_execute(operation, **kwargs)

        driver._session.execute = hang_execute  # type: ignore[method-assign]
        timed_out = await driver.update_point_table(
            make_table("ads", _power_with_resolved_address(driver), _speed()),
            timeout=0.1,
        )
        assert timed_out.status is PointTableUpdateStatus.TIMEOUT
        assert timed_out.applied is False
        assert timed_out.active_version == 1
        assert driver._point_table is original_table
        await subscription.stop()
    finally:
        await _teardown(server, driver)


async def test_st_56_concurrent_notification_delivery_survives_hot_update() -> None:
    host_ip = "127.0.0.42"
    server, driver = await _stack(
        host_ip, "ads-st56", (_power(), _speed()), make_table("ads", _power())
    )
    try:
        received = asyncio.Event()
        values: list = []

        async def callback(batch: tuple) -> None:
            values.extend(batch)
            received.set()

        subscription = driver.subscribe(["power"], callback, interval=0.05)
        await subscription.start()
        try:
            server.update(random.Random(56))
            await asyncio.wait_for(received.wait(), timeout=3.0)
            delivered_before = len(values)
            assert delivered_before >= 1

            # 持续上送与热更新并发：后台不断触发服务端值变化。
            async def pump() -> None:
                rng = random.Random(7)
                for _ in range(10):
                    server.update(rng)
                    await asyncio.sleep(0.05)

            pump_task = asyncio.create_task(pump())
            result = await driver.update_point_table(
                make_table("ads", _power_with_resolved_address(driver), _speed()),
                timeout=5.0,
            )
            await pump_task
            assert result.status is PointTableUpdateStatus.SUCCESS
            assert result.active_version == 2

            # 代次切换后订阅仍存活、继续收数，且投递均为当前代次 power 样本。
            received.clear()
            server.update(random.Random(57))
            await asyncio.wait_for(received.wait(), timeout=3.0)
            assert all(getattr(item, "point_id", None) == "power" for item in values)
            assert len(values) > delivered_before
        finally:
            await subscription.stop()
        # 停止后句柄完全收敛，无预备/退役残留。
        assert subscription._handles == []
        assert subscription._retired_handles == []
        assert subscription._prepared_handles == []
    finally:
        await _teardown(server, driver)
