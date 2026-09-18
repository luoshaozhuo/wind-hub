"""Unit tests for ADS driver — mocked pyads Connection."""

from __future__ import annotations

import pytest

from wind_hub.adapter.outbound.protocol.ads.driver import ADSDriver
from wind_hub.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub.domain.model.command import Command
from wind_hub.domain.model.errors import ProtocolError
from wind_hub.domain.model.point import PointRef, Quality


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

    def read_by_name(self, symbol: str, plc_datatype: object) -> object:
        if not self.is_open:
            raise RuntimeError("connection is closed")
        return self.symbol_values.get(symbol)

    def write(
        self, index_group: int, index_offset: int, value: object, plc_datatype: object
    ) -> None:
        if not self.is_open:
            raise RuntimeError("connection is closed")
        self.index_write_calls += 1
        self.values[(index_group, index_offset)] = value

    def write_by_name(self, symbol: str, value: object, plc_datatype: object) -> None:
        if not self.is_open:
            raise RuntimeError("connection is closed")
        self.symbol_values[symbol] = value


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyads.Connection", FakeConnection)


# ---------------------------------------------------------------------------
# connect / close / health
# ---------------------------------------------------------------------------


class TestConnect:
    async def test_connect_and_close(self, patched: None, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = ADSDriver(_make_device_config(ams_net_id="192.168.0.100.1.1"))
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
        driver = ADSDriver(_make_device_config(ams_net_id="192.168.0.100.1.1", target_port=900))
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
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping([_make_point_config("speed", "float32")])
        await driver.connect()
        driver._connection.values[(0x4020, 0)] = 42.5

        values = await driver.read([PointRef(device_id="test-dev", point_id="speed")])
        await driver.close()

        assert values[0].value == 42.5
        assert values[0].quality == Quality.GOOD
        assert values[0].source == "ads"

    async def test_read_bool(self, patched: None, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping([_make_point_config("flag", "bool", index_offset=4)])
        await driver.connect()
        driver._connection.values[(0x4020, 4)] = True

        values = await driver.read([PointRef(device_id="test-dev", point_id="flag")])
        await driver.close()

        assert values[0].value is True

    async def test_read_unknown_point_bad(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
        await driver.connect()

        values = await driver.read([PointRef(device_id="test-dev", point_id="no.point")])
        await driver.close()

        assert values[0].value is None
        assert values[0].quality == Quality.BAD

    async def test_read_not_connected_raises(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.read([PointRef(device_id="test-dev", point_id="p")])

    async def test_sequential_read_prefers_symbol(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """symbol+index 同时存在时，sequential 读用 read_by_name（symbol 优先）。"""
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping([_make_point_config("speed", "float32", symbol="MAIN.speed")])
        await driver.connect()
        conn = driver._connection
        conn.symbol_values["MAIN.speed"] = 1200.5
        conn.values[(0x4020, 0)] = -1.0  # 若误走 index 读会拿到该值

        values = await driver.read([PointRef(device_id="test-dev", point_id="speed")])
        await driver.close()

        assert values[0].value == 1200.5
        assert values[0].quality == Quality.GOOD
        assert conn.index_read_calls == 0  # 未走 index 读

    async def test_sequential_read_uses_index_without_symbol(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只有 index 对时，sequential 读用 index read。"""
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
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
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
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
            def read_by_name(self, symbol: str, plc_datatype: object) -> object:
                if symbol == "MAIN.missing":
                    raise pyads.ADSError(1808, "symbol not found")
                return super().read_by_name(symbol, plc_datatype)

        monkeypatch.setattr("pyads.Connection", _SymbolErrorConnection)
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
        driver.set_points_mapping(
            [
                _make_point_config("speed", "float32", symbol="MAIN.speed"),
                _make_point_config("missing", "float32", symbol="MAIN.missing"),
                _make_point_config("temp", "float32", symbol="MAIN.temp"),
            ]
        )
        await driver.connect()
        conn = driver._connection
        conn.symbol_values["MAIN.speed"] = 1200.5
        conn.symbol_values["MAIN.temp"] = 65.0

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
            def read_by_name(self, symbol: str, plc_datatype: object) -> object:
                raise pyads.ADSError(1861, "timeout elapsed")

        monkeypatch.setattr("pyads.Connection", _TimeoutConnection)
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
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
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
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
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
        await driver.connect()

        results = await driver.write(
            [Command(command_id="c1", device_id="test-dev", point_id="nope", value=1.0)]
        )
        await driver.close()

        assert results[0].success is False
        assert "unknown point" in (results[0].error or "")

    async def test_write_not_connected_raises(self, patched: None) -> None:
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.write(
                [Command(command_id="c1", device_id="test-dev", point_id="p", value=1.0)]
            )

    async def test_write_empty_returns_empty(self) -> None:
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
        assert await driver.write([]) == []

    async def test_write_prefers_symbol(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """symbol 存在时写入调 write_by_name，不调 index write。"""
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
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
        assert conn.symbol_values["MAIN.setpoint"] == 88.0
        assert conn.index_write_calls == 0  # 未走 index 写

    async def test_write_uses_index_without_symbol(
        self, patched: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 symbol 时写入调 index write。"""
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
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
        driver = ADSDriver(_make_device_config(ams_net_id="1.1.1.1.1.1"))
        with pytest.raises(NotImplementedError):
            await driver.subscribe([], lambda v: None)  # type: ignore[arg-type]


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
