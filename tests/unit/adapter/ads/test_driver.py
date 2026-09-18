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
) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        device_id="test-dev",
        address=PointAddress(index_group=index_group, index_offset=index_offset),
        data_type=data_type,
    )


class FakeConnection:
    """Synchronous stand-in for ``pyads.Connection`` (accessed via threads)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.is_open = False
        self.values: dict[tuple[int, int], object] = {}
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
        return self.values.get((index_group, index_offset))

    def write(
        self, index_group: int, index_offset: int, value: object, plc_datatype: object
    ) -> None:
        if not self.is_open:
            raise RuntimeError("connection is closed")
        self.values[(index_group, index_offset)] = value


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
