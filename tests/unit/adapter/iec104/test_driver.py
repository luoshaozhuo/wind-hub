"""Unit tests for IEC104 driver — point mapping, read, health.

Tests aspects of the driver that can be verified without a real
TCP connection.
"""

from __future__ import annotations

import pytest

from wind_hub.adapter.outbound.protocol.iec104.driver import IEC104Driver
from wind_hub.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub.domain.model.point import PointRef, PointValue

# ---------------------------------------------------------------------------
# helper — build a minimal DeviceConfig for IEC104
# ---------------------------------------------------------------------------


def _make_device_config(**extensions: object) -> DeviceConfig:
    return DeviceConfig(
        device_id="test-device",
        protocol="iec104",
        point_table="t1",
        endpoint=Endpoint(
            host="127.0.0.1",
            port=2404,
            extensions=dict(extensions),
        ),
    )


def _make_point_config(
    point_id: str,
    ioa: int,
) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(ioa=ioa),
        data_type="float32",
    )


# ===========================================================================
# configuration
# ===========================================================================


class TestDriverConfig:
    def test_default_config(self) -> None:
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        iece_cfg = driver._cfg
        assert iece_cfg.host == "127.0.0.1"
        assert iece_cfg.port == 2404
        assert iece_cfg.common_addr == 1
        assert iece_cfg.k == 12
        assert iece_cfg.w == 8
        assert iece_cfg.t0 == 30.0
        assert iece_cfg.t1 == 15.0
        assert iece_cfg.t2 == 10.0
        assert iece_cfg.t3 == 20.0
        assert iece_cfg.max_reconnect_retries == 5

    def test_custom_config(self) -> None:
        cfg = _make_device_config(
            common_addr=10,
            k=8,
            w=6,
            t0=5.0,
            t1=3.0,
            t2=2.0,
            t3=10.0,
            max_reconnect_retries=3,
        )
        driver = IEC104Driver(cfg)
        iece_cfg = driver._cfg
        assert iece_cfg.common_addr == 10
        assert iece_cfg.k == 8
        assert iece_cfg.w == 6
        assert iece_cfg.t0 == 5.0
        assert iece_cfg.t1 == 3.0
        assert iece_cfg.t2 == 2.0
        assert iece_cfg.t3 == 10.0
        assert iece_cfg.max_reconnect_retries == 3


# ===========================================================================
# point mapping
# ===========================================================================


class TestPointMapping:
    def test_set_points_mapping(self) -> None:
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        points = [
            _make_point_config("rotor.speed", 100),
            _make_point_config("gen.power", 200),
        ]
        driver.set_points_mapping(points)

        assert driver._ioa_to_point_id == {100: "rotor.speed", 200: "gen.power"}
        assert driver._point_id_to_ioa == {"rotor.speed": 100, "gen.power": 200}

    def test_duplicate_ioa_warns(self) -> None:
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        points = [
            _make_point_config("a", 100),
            _make_point_config("b", 100),
        ]
        # Should not raise — last one wins, warning logged.
        driver.set_points_mapping(points)
        # Last one wins.
        assert driver._ioa_to_point_id[100] == "b"

    def test_missing_ioa_skips(self) -> None:
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        points = [
            PointConfig(
                point_groups=["default"],
                point_id="bad",
                address=PointAddress(),  # No IOA field.
                data_type="float32",
            ),
            _make_point_config("good", 200),
        ]
        driver.set_points_mapping(points)
        assert driver._ioa_to_point_id == {200: "good"}
        assert driver._point_id_to_ioa == {"good": 200}


# ===========================================================================
# read — without connection (should raise)
# ===========================================================================


class TestReadNotConnected:
    async def test_read_raises_when_not_connected(self) -> None:
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        points = [PointRef(device_id="test-device", point_id="rotor.speed")]

        from wind_hub.domain.model.errors import ProtocolError

        with pytest.raises(ProtocolError, match="not connected"):
            await driver.read(points)


# ===========================================================================
# write / subscribe — implemented (step 7b-3)
# ===========================================================================


class TestWriteAndSubscribe:
    async def test_write_empty_list_returns_empty(self) -> None:
        """An empty command list is a no-op, even when not connected."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)

        assert await driver.write([]) == []

    async def test_subscribe_registers_without_connection(self) -> None:
        """subscribe() registers a callback without requiring a connection."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)

        async def cb(v: PointValue) -> None:
            pass

        # No connection, no error — the callback is stored in the registry.
        await driver.subscribe([], cb)
        assert driver._subscriptions.global_count == 1


# ===========================================================================
# health
# ===========================================================================


class TestHealth:
    def test_health_not_connected(self) -> None:
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        health = driver.health()
        assert not health.healthy

    def test_health_failed(self) -> None:
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver._failed = True
        health = driver.health()
        assert not health.healthy
        assert "FAILED" in (health.message or "")


# ===========================================================================
# self-registration
# ===========================================================================


class TestSelfRegistration:
    def test_factory_creates_driver(self) -> None:
        from wind_hub.adapter.outbound.protocol.iec104.driver import (
            _create_iec104,
        )

        cfg = _make_device_config()
        driver = _create_iec104(cfg)
        assert isinstance(driver, IEC104Driver)
        assert driver._cfg.host == "127.0.0.1"
        assert driver._cfg.port == 2404


# ===========================================================================
# 重连循环日志（决策 2）：超时降级为简洁 warning，其他异常保留堆栈
# ===========================================================================


import logging  # noqa: E402

import wind_hub.adapter.outbound.protocol.iec104.driver as iec104_driver_module  # noqa: E402
from wind_hub.adapter.outbound.protocol.iec104.driver import _is_timeout_related  # noqa: E402
from wind_hub.domain.model.errors import ProtocolError  # noqa: E402


class _FakeSessionBase:
    """替代 IEC104Session：构造/配置方法为空实现，start 由子类注入异常。"""

    def __init__(self, **kwargs: object) -> None:
        pass

    def set_points_mapping(self, *args: object) -> None:
        pass

    def set_on_asdu(self, *args: object) -> None:
        pass


class TestReconnectLogging:
    def test_is_timeout_related_detects_wrapped_cause(self) -> None:
        exc = ProtocolError("IEC104: TCP connect failed")
        exc.__cause__ = TimeoutError("Connection timed out")
        assert _is_timeout_related(exc) is True

    def test_is_timeout_related_detects_handshake_message(self) -> None:
        exc = ProtocolError("IEC104: STARTDT handshake timed out after 15.0s")
        assert _is_timeout_related(exc) is True

    def test_is_timeout_related_rejects_other_errors(self) -> None:
        assert _is_timeout_related(ProtocolError("IEC104: unexpected frame")) is False

    async def test_monitor_loop_logs_timeout_without_traceback(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(iec104_driver_module, "_RECONNECT_BACKOFF_BASE", 0.0)

        class _TimeoutSession(_FakeSessionBase):
            async def start(self) -> None:
                raise ProtocolError("IEC104: STARTDT handshake timed out after 15.0s")

        monkeypatch.setattr(iec104_driver_module, "IEC104Session", _TimeoutSession)
        driver = IEC104Driver(_make_device_config(max_reconnect_retries=1))

        with caplog.at_level(logging.WARNING, logger=iec104_driver_module.__name__):
            await driver._monitor_loop()  # noqa: SLF001

        timeout_logs = [r for r in caplog.records if "reconnect timed out" in r.message]
        assert len(timeout_logs) == 1
        assert timeout_logs[0].levelno == logging.WARNING
        assert timeout_logs[0].exc_info is None  # 降级：无堆栈

    async def test_monitor_loop_keeps_traceback_for_non_timeout(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(iec104_driver_module, "_RECONNECT_BACKOFF_BASE", 0.0)

        class _ErrorSession(_FakeSessionBase):
            async def start(self) -> None:
                raise ProtocolError("IEC104: unexpected frame")

        monkeypatch.setattr(iec104_driver_module, "IEC104Session", _ErrorSession)
        driver = IEC104Driver(_make_device_config(max_reconnect_retries=1))

        with caplog.at_level(logging.WARNING, logger=iec104_driver_module.__name__):
            await driver._monitor_loop()  # noqa: SLF001

        fail_logs = [r for r in caplog.records if "reconnect failed" in r.message]
        assert len(fail_logs) == 1
        assert fail_logs[0].levelno == logging.ERROR
        assert fail_logs[0].exc_info is not None  # 其他异常保留完整堆栈
