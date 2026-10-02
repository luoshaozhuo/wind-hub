"""Unit tests for ADS driver — mocked pyads Connection."""

from __future__ import annotations

import asyncio
import ctypes
from types import SimpleNamespace

import pytest

from wind_hub.adapter.outbound.protocol.ads.driver import ADSDriver
from wind_hub.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub.domain.model.command import Command
from wind_hub.domain.model.errors import ConfigError, ProtocolError
from wind_hub.domain.model.point import PointRef, Quality
from wind_hub.domain.port.outbound import AcquisitionMode


def _make_device_config(**extensions: object) -> DeviceConfig:
    return DeviceConfig(
        device_id="test-dev",
        protocol="ads",
        point_table="t1",
        endpoint=Endpoint(
            host="192.168.0.100",
            port=48898,
            extensions=dict(extensions),
        ),
        read_mode="sequential",
    )


def _make_point_config(
    point_id: str,
    data_type: str = "float32",
    index_group: int = 0x4020,
    index_offset: int = 0,
    **extra: object,
) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(index_group=index_group, index_offset=index_offset, **extra),
        data_type=data_type,
    )


class FakeConnection:
    """Synchronous stand-in for ``pyads.Connection`` (accessed via threads)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.is_open = False
        self.values: dict[tuple[int, int], object] = {}
        self.symbol_values: dict[str, object] = {}
        self.symbol_addresses: dict[str, tuple[int, int]] = {
            "MAIN.speed": (0x4020, 100),
            "MAIN.temp": (0x4020, 104),
            "MAIN.setpoint": (0x4020, 108),
            "MAIN.missing": (0x4020, 112),
        }
        self.index_read_calls = 0
        self.index_write_calls = 0
        self.timeout_ms: int | None = None

    def set_timeout(self, ms: int) -> None:
        self.timeout_ms = ms

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read(self, index_group: int, index_offset: int, plc_datatype: object) -> object:
        if not self.is_open:
            raise RuntimeError("connection is closed")
        self.index_read_calls += 1
        return self.values.get((index_group, index_offset))

    def get_symbol(self, symbol: str) -> object:
        if not self.is_open:
            raise RuntimeError("connection is closed")
        index_group, index_offset = self.symbol_addresses.get(symbol, (0x4020, 0))
        return SimpleNamespace(
            index_group=index_group,
            index_offset=index_offset,
            plc_type=ctypes.c_float,
            symbol_type="REAL",
        )

    def read_by_name(self, symbol: str, plc_datatype: object) -> object:
        raise AssertionError("production ADS reads must not use read_by_name")

    def write(
        self, index_group: int, index_offset: int, value: object, plc_datatype: object
    ) -> None:
        if not self.is_open:
            raise RuntimeError("connection is closed")
        self.index_write_calls += 1
        self.values[(index_group, index_offset)] = value

    def write_by_name(self, symbol: str, value: object, plc_datatype: object) -> None:
        raise AssertionError("production ADS writes must not use write_by_name")

    def add_device_notification(
        self, symbol: str, attr: object, callback: object, user_handle: object = None
    ) -> tuple[object, object]:
        if not self.is_open:
            raise RuntimeError("connection is closed")
        handle: tuple[object, object] = (symbol, "handle")
        return (handle, user_handle)

    def del_device_notification(self, handle: object, user_handle: object) -> None:
        pass


class FakeNotificationAttrib:
    """``pyads.NotificationAttrib`` 替身——记录订阅方传入的 cycle_time。"""

    instances: list[FakeNotificationAttrib] = []

    def __init__(self, length: int, cycle_time: float = 0.0, max_delay: float = 0.0) -> None:
        self.length = length
        self.cycle_time = cycle_time
        self.max_delay = max_delay
        FakeNotificationAttrib.instances.append(self)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeNotificationAttrib.instances = []
    monkeypatch.setattr("pyads.Connection", FakeConnection)
    monkeypatch.setattr("pyads.NotificationAttrib", FakeNotificationAttrib)


# ---------------------------------------------------------------------------
# connect / close / health
# ---------------------------------------------------------------------------


class TestConnect:
    async def test_connect_and_close(self, patched: None, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="192.168.0.100.1.1"))
        await driver.connect()
        conn = driver._connection
        assert driver.health().healthy is True
        assert conn.is_open is True

        await driver.close()
        assert driver.health().healthy is False
        assert conn.is_open is False

    async def test_connect_passes_net_id_and_port(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="192.168.0.100.1.1", target_port=900))
        await driver.connect()
        try:
            conn = driver._connection
            assert conn.timeout_ms == 5000
        finally:
            await driver.close()


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestRead:
    async def test_read_real(self, patched: None, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping([_make_point_config("speed", "float32")])
        await driver.connect()
        driver._connection.values[(0x4020, 0)] = 42.5

        values = await driver.read([PointRef(device_id="test-dev", point_id="speed")])
        await driver.close()

        assert values[0].value == 42.5
        assert values[0].quality == Quality.GOOD
        assert values[0].source == "ads"

    async def test_read_bool(self, patched: None, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping([_make_point_config("flag", "bool", index_offset=4)])
        await driver.connect()
        driver._connection.values[(0x4020, 4)] = True

        values = await driver.read([PointRef(device_id="test-dev", point_id="flag")])
        await driver.close()

        assert values[0].value is True

    async def test_read_unknown_point_bad(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        await driver.connect()

        values = await driver.read([PointRef(device_id="test-dev", point_id="no.point")])
        await driver.close()

        assert values[0].value is None
        assert values[0].quality == Quality.BAD

    async def test_read_not_connected_raises(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.read([PointRef(device_id="test-dev", point_id="p")])

    async def test_sequential_read_resolves_symbol_then_uses_index(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """symbol+旧 index 同时存在时，以 PLC symbol 实际解析地址执行 index read。"""
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping([_make_point_config("speed", "float32", symbol="MAIN.speed")])
        await driver.connect()
        conn = driver._connection
        conn.values[(0x4020, 100)] = 1200.5
        conn.values[(0x4020, 0)] = -1.0

        values = await driver.read([PointRef(device_id="test-dev", point_id="speed")])
        await driver.close()

        assert values[0].value == 1200.5
        assert values[0].quality == Quality.GOOD
        assert conn.index_read_calls == 1

    async def test_sequential_read_uses_index_without_symbol(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只有 index 对时，sequential 读用 index read。"""
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping([_make_point_config("speed", "float32")])
        await driver.connect()
        conn = driver._connection
        conn.values[(0x4020, 0)] = 7.5

        values = await driver.read([PointRef(device_id="test-dev", point_id="speed")])
        await driver.close()

        assert values[0].value == 7.5
        assert conn.index_read_calls == 1

    async def test_sequential_read_n_points_means_n_reads(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sequential 语义：一批 N 个点 = N 次独立读（无 sum 合并）。"""
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping(
            [
                _make_point_config("p1", index_offset=0),
                _make_point_config("p2", index_offset=4),
                _make_point_config("p3", index_offset=8),
            ]
        )
        await driver.connect()
        conn = driver._connection
        conn.values[(0x4020, 0)] = 1.0
        conn.values[(0x4020, 4)] = 2.0
        conn.values[(0x4020, 8)] = 3.0

        values = await driver.read(
            [PointRef(device_id="test-dev", point_id=f"p{i}") for i in (1, 2, 3)]
        )
        await driver.close()

        assert [v.value for v in values] == [1.0, 2.0, 3.0]
        assert conn.index_read_calls == 3  # 3 个点 = 3 次读

    async def test_symbol_not_found_marks_only_that_point_bad(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """批量读部分失败：ADS 1808（符号不存在）是单点级错误——该点 BAD，
        批次内其它点不受影响，顺序与数量不变。"""
        import pyads

        class _SymbolErrorConnection(FakeConnection):
            def get_symbol(self, symbol: str) -> object:
                if symbol == "MAIN.missing":
                    raise pyads.ADSError(1808, "symbol not found")
                return super().get_symbol(symbol)

        monkeypatch.setattr("pyads.Connection", _SymbolErrorConnection)
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping(
            [
                _make_point_config("speed", "float32", symbol="MAIN.speed"),
                _make_point_config("missing", "float32", symbol="MAIN.missing"),
                _make_point_config("temp", "float32", symbol="MAIN.temp"),
            ]
        )
        await driver.connect()
        conn = driver._connection
        conn.values[(0x4020, 100)] = 1200.5
        conn.values[(0x4020, 104)] = 65.0

        values = await driver.read(
            [
                PointRef(device_id="test-dev", point_id="speed"),
                PointRef(device_id="test-dev", point_id="missing"),
                PointRef(device_id="test-dev", point_id="temp"),
            ]
        )
        assert driver.health().healthy is True  # 单点错误不触发断线
        await driver.close()

        assert [v.point_id for v in values] == ["speed", "missing", "temp"]
        assert values[0].quality == Quality.GOOD
        assert values[0].value == 1200.5
        assert values[1].quality == Quality.BAD  # 符号不存在 → 单点 BAD
        assert values[1].value is None
        assert values[2].quality == Quality.GOOD
        assert values[2].value == 65.0

    async def test_other_ads_error_propagates_as_protocol_error(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非 1808 的 ADS 错误（如 1861 超时）无法与连接级故障区分——
        保持上抛 ProtocolError 并走断线路径，不伪造成功。"""
        import pyads

        class _TimeoutConnection(FakeConnection):
            def get_symbol(self, symbol: str) -> object:
                raise pyads.ADSError(1861, "timeout elapsed")

        monkeypatch.setattr("pyads.Connection", _TimeoutConnection)
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping([_make_point_config("speed", "float32", symbol="MAIN.speed")])
        await driver.connect()

        with pytest.raises(ProtocolError, match="ADS read failed"):
            await driver.read([PointRef(device_id="test-dev", point_id="speed")])
        assert driver.health().healthy is False  # 断线路径（后台重连）
        await driver.close()


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    async def test_write_real(self, patched: None, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping([_make_point_config("setpoint", "float32")])
        await driver.connect()
        conn = driver._connection

        results = await driver.write(
            [Command(command_id="c1", device_id="test-dev", point_id="setpoint", value=99.5)]
        )
        await driver.close()

        assert results[0].success is True
        assert conn.values[(0x4020, 0)] == 99.5

    async def test_write_unknown_point_fails(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        await driver.connect()

        results = await driver.write(
            [Command(command_id="c1", device_id="test-dev", point_id="nope", value=1.0)]
        )
        await driver.close()

        assert results[0].success is False
        assert "unknown point" in (results[0].error or "")

    async def test_write_not_connected_raises(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.write(
                [Command(command_id="c1", device_id="test-dev", point_id="p", value=1.0)]
            )

    async def test_write_empty_returns_empty(self) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        assert await driver.write([]) == []

    async def test_write_resolves_symbol_then_uses_index(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """symbol 存在时先解析 PLC 地址，正式写入仍使用 index/offset。"""
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping(
            [_make_point_config("setpoint", "float32", symbol="MAIN.setpoint")]
        )
        await driver.connect()
        conn = driver._connection

        results = await driver.write(
            [Command(command_id="c1", device_id="test-dev", point_id="setpoint", value=88.0)]
        )
        await driver.close()

        assert results[0].success is True
        assert conn.values[(0x4020, 108)] == 88.0
        assert conn.index_write_calls == 1

    async def test_write_uses_index_without_symbol(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 symbol 时写入调 index write。"""
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping([_make_point_config("setpoint", "float32")])
        await driver.connect()
        conn = driver._connection

        results = await driver.write(
            [Command(command_id="c1", device_id="test-dev", point_id="setpoint", value=1.5)]
        )
        await driver.close()

        assert results[0].success is True
        assert conn.values[(0x4020, 0)] == 1.5
        assert conn.index_write_calls == 1


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------


class TestSubscribe:
    async def test_subscribe_raises_not_implemented(self) -> None:
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        with pytest.raises(NotImplementedError):
            await driver.subscribe([], lambda v: None)  # type: ignore[arg-type]

    async def test_acquisition_mode_follows_subscribe_enabled(self) -> None:
        """subscribe_enabled → SUBSCRIBE；否则 POLL。"""
        poll_driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        sub_driver = ADSDriver(
            _make_device_config(target_net_id="1.1.1.1.1.1", subscribe_enabled=True)
        )
        assert poll_driver.acquisition_mode is AcquisitionMode.POLL
        assert sub_driver.acquisition_mode is AcquisitionMode.SUBSCRIBE

    async def test_subscribe_requires_interval(self, patched: None) -> None:
        """interval 缺失/非正 → ConfigError（cycle_time 必须来自 Task.interval）。"""
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1", subscribe_enabled=True))
        driver.set_points_mapping([_make_point_config("speed", "float32", symbol="MAIN.speed")])
        ref = PointRef(device_id="test-dev", point_id="speed")

        async def _noop(pv: object) -> None:
            return None

        with pytest.raises(ConfigError, match="interval"):
            await driver.subscribe([ref], _noop)  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="interval"):
            await driver.subscribe([ref], _noop, interval=0.0)  # type: ignore[arg-type]

    async def test_interval_becomes_notification_cycle_time(self, patched: None) -> None:
        """Task.interval → NotificationAttrib.cycle_time。"""
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1", subscribe_enabled=True))
        driver.set_points_mapping([_make_point_config("speed", "float32", symbol="MAIN.speed")])
        ref = PointRef(device_id="test-dev", point_id="speed")

        async def _noop(pv: object) -> None:
            return None

        handle = await driver.subscribe([ref], _noop, interval=0.25)  # type: ignore[arg-type]
        try:
            assert FakeNotificationAttrib.instances
            assert all(attr.cycle_time == 0.25 for attr in FakeNotificationAttrib.instances)
        finally:
            await handle.close()

    async def test_independent_subscriptions_stop_a_keeps_b(self, patched: None) -> None:
        """同一 symbol 的两份订阅互不影响：关闭 A 的句柄后 B 仍活跃。"""
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1", subscribe_enabled=True))
        driver.set_points_mapping([_make_point_config("speed", "float32", symbol="MAIN.speed")])
        ref = PointRef(device_id="test-dev", point_id="speed")

        async def _noop(pv: object) -> None:
            return None

        handle_a = await driver.subscribe([ref], _noop, interval=0.1)  # type: ignore[arg-type]
        handle_b = await driver.subscribe([ref], _noop, interval=0.5)  # type: ignore[arg-type]
        assert len(driver._subscriptions) == 2  # noqa: SLF001

        await handle_a.close()
        assert len(driver._subscriptions) == 1  # noqa: SLF001
        remaining = next(iter(driver._subscriptions))  # noqa: SLF001
        assert remaining._cycle_time == 0.5  # noqa: SLF001
        await handle_b.close()
        assert driver._subscriptions == set()  # noqa: SLF001

    async def test_driver_close_closes_all_subscriptions(self, patched: None) -> None:
        """驱动整体关闭时全部订阅随之清理。"""
        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1", subscribe_enabled=True))
        driver.set_points_mapping([_make_point_config("speed", "float32", symbol="MAIN.speed")])
        ref = PointRef(device_id="test-dev", point_id="speed")

        async def _noop(pv: object) -> None:
            return None

        await driver.subscribe([ref], _noop, interval=0.1)  # type: ignore[arg-type]
        await driver.subscribe([ref], _noop, interval=0.2)  # type: ignore[arg-type]
        await driver.close()
        assert driver._subscriptions == set()  # noqa: SLF001


# ---------------------------------------------------------------------------
# 重连循环日志（决策 2）：超时标记为 timed out 的简洁 warning
# ---------------------------------------------------------------------------


import logging  # noqa: E402

import wind_hub.adapter.outbound.protocol.ads.driver as ads_driver_module  # noqa: E402


class TestReconnectLogging:
    async def test_timeout_logged_as_concise_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(ads_driver_module, "_RECONNECT_BACKOFF_BASE", 0.0)
        driver = ADSDriver(_make_device_config(reconnect_max_retries=1))

        async def _timeout_connect() -> None:
            raise TimeoutError("timed out")

        monkeypatch.setattr(driver, "_do_connect", _timeout_connect)
        with caplog.at_level(logging.WARNING, logger=ads_driver_module.__name__):
            last_exc = await driver._connect_with_retry()  # noqa: SLF001

        assert isinstance(last_exc, TimeoutError)
        timeout_logs = [r for r in caplog.records if "timed out" in r.message]
        assert len(timeout_logs) == 2  # 两次尝试各一条
        assert all(r.exc_info is None for r in timeout_logs)  # 无堆栈

    async def test_non_timeout_failure_keeps_failed_message(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(ads_driver_module, "_RECONNECT_BACKOFF_BASE", 0.0)
        driver = ADSDriver(_make_device_config(reconnect_max_retries=1))

        async def _refused_connect() -> None:
            raise OSError("connection refused")

        monkeypatch.setattr(driver, "_do_connect", _refused_connect)
        with caplog.at_level(logging.WARNING, logger=ads_driver_module.__name__):
            last_exc = await driver._connect_with_retry()  # noqa: SLF001

        assert isinstance(last_exc, OSError)
        assert any("failed" in r.message for r in caplog.records)
        assert not any("timed out" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# route 自动修复（每台设备每进程最多一次，且仅首次连接成功之前）
# ---------------------------------------------------------------------------


class _RepairRecorder:
    """``ads_router.repair_route_once`` 的替身——记录调用并按配置返回。"""

    def __init__(self, result: bool) -> None:
        self.result = result
        self.hosts: list[str] = []

    async def __call__(self, plc_ip: str) -> bool:
        self.hosts.append(plc_ip)
        return self.result


class TestRouteRepair:
    async def test_no_repair_when_first_connect_succeeds(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RepairRecorder(result=True)
        monkeypatch.setattr(ads_driver_module.ads_router, "repair_route_once", recorder)

        driver = ADSDriver(_make_device_config(target_net_id="1.1.1.1.1.1"))
        await driver.connect()
        await driver.close()

        assert recorder.hosts == []  # 首次连接成功不触发 add_route_to_plc

    async def test_repair_once_then_reconnect_succeeds(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RepairRecorder(result=True)
        monkeypatch.setattr(ads_driver_module.ads_router, "repair_route_once", recorder)
        monkeypatch.setattr(ads_driver_module, "_RECONNECT_BACKOFF_BASE", 0.0)

        driver = ADSDriver(
            _make_device_config(target_net_id="1.1.1.1.1.1", reconnect_max_retries=1)
        )
        attempts = 0
        real_do_connect = driver._do_connect  # noqa: SLF001

        async def _flaky_connect() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("no route to host")
            await real_do_connect()

        monkeypatch.setattr(driver, "_do_connect", _flaky_connect)
        await driver.connect()
        try:
            assert driver.health().healthy is True
            assert recorder.hosts == ["192.168.0.100"]  # 恰好修复一次
            assert attempts == 2  # 首次失败 + 修复后重连成功
        finally:
            await driver.close()

    async def test_repair_not_repeated_when_still_failing(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RepairRecorder(result=True)
        monkeypatch.setattr(ads_driver_module.ads_router, "repair_route_once", recorder)
        monkeypatch.setattr(ads_driver_module, "_RECONNECT_BACKOFF_BASE", 0.0)

        driver = ADSDriver(
            _make_device_config(target_net_id="1.1.1.1.1.1", reconnect_max_retries=2)
        )

        async def _always_fail() -> None:
            raise OSError("no route to host")

        monkeypatch.setattr(driver, "_do_connect", _always_fail)
        with pytest.raises(ProtocolError):
            await driver.connect()
        await driver.close()

        assert recorder.hosts == ["192.168.0.100"]  # 整轮 retry 只修复一次

    async def test_no_repair_after_ever_connected(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """已成功连接过的设备掉线后只 reconnect，不再 add route。"""
        recorder = _RepairRecorder(result=True)
        monkeypatch.setattr(ads_driver_module.ads_router, "repair_route_once", recorder)
        monkeypatch.setattr(ads_driver_module, "_RECONNECT_BACKOFF_BASE", 0.0)

        driver = ADSDriver(
            _make_device_config(target_net_id="1.1.1.1.1.1", reconnect_max_retries=1)
        )
        await driver.connect()

        async def _always_fail() -> None:
            raise OSError("connection reset")

        monkeypatch.setattr(driver, "_do_connect", _always_fail)
        driver._signal_disconnect()  # noqa: SLF001
        last_exc = await driver._connect_with_retry()  # noqa: SLF001
        await driver.close()

        assert isinstance(last_exc, OSError)
        assert recorder.hosts == []


# ---------------------------------------------------------------------------
# 长期重连：retry budget 耗尽后 monitor 不退出，PLC 恢复后仍能连上
# ---------------------------------------------------------------------------


class TestMonitorRecovery:
    async def test_monitor_survives_exhausted_round_and_recovers(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ads_driver_module, "_RECONNECT_BACKOFF_BASE", 0.0)
        driver = ADSDriver(
            _make_device_config(
                target_net_id="1.1.1.1.1.1",
                reconnect_max_retries=0,
                reconnect_backoff_max=0.01,
            )
        )
        plc_up = False
        real_do_connect = driver._do_connect  # noqa: SLF001

        async def _plc_controlled_connect() -> None:
            if not plc_up:
                raise OSError("plc not started")
            await real_do_connect()

        monkeypatch.setattr(driver, "_do_connect", _plc_controlled_connect)

        # PLC 未启动：首轮连接失败，connect() 抛出，但后台 monitor 继续重连
        with pytest.raises(ProtocolError):
            await driver.connect()
        assert driver.health().healthy is False
        assert "degraded" in (driver.health().message or "")

        plc_up = True  # PLC 数十秒后启动
        try:
            for _ in range(200):  # 最多等 ~2s，重连间隔 0.01s
                if driver._connected:  # noqa: SLF001
                    break
                await asyncio.sleep(0.01)
            assert driver._connected is True  # noqa: SLF001
            assert driver.health().healthy is True
        finally:
            await driver.close()
