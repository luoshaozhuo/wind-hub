"""Unit tests for IEC104 driver — write / remote control."""

from __future__ import annotations

import pytest

from wind_hub.adapter.outbound.protocol.iec104.codec.info_objects import (
    DoubleCommand,
    SetpointCommandShort,
    SingleCommand,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.types import (
    CauseOfTransmission,
    TypeID,
)
from wind_hub.adapter.outbound.protocol.iec104.driver import IEC104Driver
from wind_hub_core.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub_core.model.command import Command
from wind_hub_core.model.errors import ProtocolError
from wind_hub_core.model.point import PointRef

# ---------------------------------------------------------------------------
# helpers
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
    data_type: str = "bool",
) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(ioa=ioa),
        data_type=data_type,
    )


def _make_cmd(
    command_id: str = "cmd-1",
    point_id: str = "p1",
    value: object = True,
) -> Command:
    return Command(
        command_id=command_id,
        device_id="d1",
        point_id=point_id,
        value=value,
    )


# ===========================================================================
# _build_control_asdu
# ===========================================================================


class TestBuildControlAsdu:
    """Tests for the private _build_control_asdu method."""

    def test_bool_value_single_point(self) -> None:
        """bool True → C_SC_NA_1 with value=True."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="bool"),
            ]
        )
        cmd = _make_cmd(value=True)
        asdu = driver._build_control_asdu(cmd, 100)
        assert asdu.type_id == TypeID.C_SC_NA_1
        assert asdu.cause == CauseOfTransmission.ACTIVATION
        assert len(asdu.objects) == 1
        obj = asdu.objects[0]
        assert isinstance(obj, SingleCommand)
        assert obj.ioa == 100
        assert obj.value is True

    def test_bool_false_single_point(self) -> None:
        """bool False → C_SC_NA_1 with value=False."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="bool"),
            ]
        )
        cmd = _make_cmd(value=False)
        asdu = driver._build_control_asdu(cmd, 100)
        assert asdu.type_id == TypeID.C_SC_NA_1
        obj = asdu.objects[0]
        assert isinstance(obj, SingleCommand)
        assert obj.value is False

    def test_float_value_set_point(self) -> None:
        """float 42.5 → C_SE_NC_1."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="float32"),
            ]
        )
        cmd = _make_cmd(value=42.5)
        asdu = driver._build_control_asdu(cmd, 100)
        assert asdu.type_id == TypeID.C_SE_NC_1
        obj = asdu.objects[0]
        assert isinstance(obj, SetpointCommandShort)
        assert obj.ioa == 100
        assert obj.value == 42.5

    def test_int_0_single_point(self) -> None:
        """int 0 → C_SC_NA_1 with value=False."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="uint16"),
            ]
        )
        cmd = _make_cmd(value=0)
        asdu = driver._build_control_asdu(cmd, 100)
        assert asdu.type_id == TypeID.C_SC_NA_1
        obj = asdu.objects[0]
        assert isinstance(obj, SingleCommand)
        assert obj.value is False

    def test_int_2_double_point(self) -> None:
        """int 2 → C_DC_NA_1 (unambiguous)."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="uint16"),
            ]
        )
        cmd = _make_cmd(value=2)
        asdu = driver._build_control_asdu(cmd, 100)
        assert asdu.type_id == TypeID.C_DC_NA_1
        obj = asdu.objects[0]
        assert isinstance(obj, DoubleCommand)
        assert obj.ioa == 100
        assert obj.value == 2

    def test_int_1_bool_data_type_single_point(self) -> None:
        """int 1 with data_type=bool → C_SC_NA_1 (data_type disambiguates)."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="bool"),
            ]
        )
        cmd = _make_cmd(value=1)
        asdu = driver._build_control_asdu(cmd, 100)
        assert asdu.type_id == TypeID.C_SC_NA_1
        obj = asdu.objects[0]
        assert isinstance(obj, SingleCommand)
        assert obj.value is True

    def test_int_1_uint_data_type_double_point(self) -> None:
        """int 1 with data_type=uint16 → C_DC_NA_1 (data_type disambiguates)."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="uint16"),
            ]
        )
        cmd = _make_cmd(value=1)
        asdu = driver._build_control_asdu(cmd, 100)
        assert asdu.type_id == TypeID.C_DC_NA_1
        obj = asdu.objects[0]
        assert isinstance(obj, DoubleCommand)
        assert obj.value == 1

    def test_int_large_set_point(self) -> None:
        """int 42 → C_SE_NC_1 (not a single/double command value)."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="int16"),
            ]
        )
        cmd = _make_cmd(value=42)
        asdu = driver._build_control_asdu(cmd, 100)
        assert asdu.type_id == TypeID.C_SE_NC_1
        obj = asdu.objects[0]
        assert isinstance(obj, SetpointCommandShort)
        assert obj.value == 42.0

    def test_float_data_type_with_int_value(self) -> None:
        """int 5 with data_type=float32 → C_SE_NC_1 (data_type-driven)."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="float32"),
            ]
        )
        cmd = _make_cmd(value=5)
        asdu = driver._build_control_asdu(cmd, 100)
        # float32 data_type triggers set-point path before int path.
        assert asdu.type_id == TypeID.C_SE_NC_1
        obj = asdu.objects[0]
        assert isinstance(obj, SetpointCommandShort)
        assert obj.value == 5.0

    def test_unsupported_value_type_raises(self) -> None:
        """str value → ValueError."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        driver.set_points_mapping(
            [
                _make_point_config("p1", 100, data_type="str"),
            ]
        )
        cmd = _make_cmd(value="hello")
        with pytest.raises(ValueError, match="unsupported value type"):
            driver._build_control_asdu(cmd, 100)


# ===========================================================================
# write — not connected
# ===========================================================================


class TestWriteNotConnected:
    async def test_write_raises_when_not_connected(self) -> None:
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.write([_make_cmd()])

    async def test_write_empty_list_returns_empty(self) -> None:
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        results = await driver.write([])
        assert results == []


# ===========================================================================
# _execute_one_command — error paths (no real session needed)
# ===========================================================================


class TestExecuteOneCommandErrors:
    """Test _execute_one_command error paths without a real connection."""

    def test_unknown_point_ioa_resolution(self) -> None:
        """Resolving an unmapped point returns None."""
        cfg = _make_device_config()
        driver = IEC104Driver(cfg)
        # No points mapped.
        ref = PointRef(device_id="d1", point_id="unknown.point")
        assert driver._resolve_ioa(ref) is None
