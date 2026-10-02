"""Tests for IEC104 information-object codecs — one section per TypeID."""

from __future__ import annotations

from wind_hub_core.protocol.iec104.codec.info_objects import (
    CounterInterrogationCommand,
    DoubleCommand,
    DoublePoint,
    DoublePointWithTime,
    InterrogationCommand,
    MeasuredValueNormalized,
    MeasuredValueNormalizedWithTime,
    MeasuredValueScaled,
    MeasuredValueShort,
    MeasuredValueShortWithTime,
    SetpointCommandShort,
    SingleCommand,
    SinglePoint,
    SinglePointWithTime,
    decode_c_ci_na_1,
    decode_c_dc_na_1,
    decode_c_ic_na_1,
    decode_c_sc_na_1,
    decode_c_se_nc_1,
    decode_m_dp_na_1,
    decode_m_dp_tb_1,
    decode_m_me_na_1,
    decode_m_me_nb_1,
    decode_m_me_nc_1,
    decode_m_me_td_1,
    decode_m_me_tf_1,
    decode_m_sp_na_1,
    decode_m_sp_tb_1,
    encode_c_ci_na_1,
    encode_c_dc_na_1,
    encode_c_ic_na_1,
    encode_c_sc_na_1,
    encode_c_se_nc_1,
    encode_m_dp_na_1,
    encode_m_dp_tb_1,
    encode_m_me_na_1,
    encode_m_me_nb_1,
    encode_m_me_nc_1,
    encode_m_me_td_1,
    encode_m_me_tf_1,
    encode_m_sp_na_1,
    encode_m_sp_tb_1,
)
from wind_hub_core.protocol.iec104.codec.time import CP56Time2a
from wind_hub_core.protocol.iec104.codec.types import QualityFlag

# Shared timestamp for tests with time
_TS = CP56Time2a(milliseconds=0, minutes=0, hours=0, day=1, month=1, year=2000)


# ==========================================================================
# M_SP_NA_1
# ==========================================================================


class TestM_SP_NA_1:
    def test_encode_on(self) -> None:
        obj = SinglePoint(ioa=0x010203, value=True)
        data = encode_m_sp_na_1(obj)
        assert data[:3] == b"\x03\x02\x01"  # IOA
        assert data[3] == 0x01  # SIQ: ON, no quality flags

    def test_encode_off(self) -> None:
        obj = SinglePoint(ioa=0x010203, value=False)
        data = encode_m_sp_na_1(obj)
        assert data[3] == 0x00  # SIQ: OFF

    def test_decode_on(self) -> None:
        data = b"\x03\x02\x01\x01"  # IOA=0x010203, SIQ=ON
        obj, off = decode_m_sp_na_1(data, 0)
        assert obj.ioa == 0x010203
        assert obj.value is True
        assert off == 4

    def test_roundtrip(self) -> None:
        obj = SinglePoint(ioa=1000, value=True, quality=QualityFlag.IV)
        result = decode_m_sp_na_1(encode_m_sp_na_1(obj), 0)[0]
        assert result == obj


# ==========================================================================
# M_DP_NA_1
# ==========================================================================


class TestM_DP_NA_1:
    def test_encode(self) -> None:
        obj = DoublePoint(ioa=100, value=2)  # ON
        data = encode_m_dp_na_1(obj)
        assert data[3] == 0x02

    def test_decode(self) -> None:
        data = b"\x64\x00\x00\x02"
        obj, off = decode_m_dp_na_1(data, 0)
        assert obj.ioa == 100
        assert obj.value == 2
        assert off == 4

    def test_roundtrip(self) -> None:
        obj = DoublePoint(ioa=2000, value=1, quality=QualityFlag.NT)
        result = decode_m_dp_na_1(encode_m_dp_na_1(obj), 0)[0]
        assert result == obj


# ==========================================================================
# M_ME_NA_1
# ==========================================================================


class TestM_ME_NA_1:
    def test_encode_one(self) -> None:
        obj = MeasuredValueNormalized(ioa=100, value=1.0)
        data = encode_m_me_na_1(obj)
        # IOA(3) + int16(2) + QDS(1) = 6 bytes
        assert len(data) == 6

    def test_encode_zero(self) -> None:
        obj = MeasuredValueNormalized(ioa=100, value=0.0)
        data = encode_m_me_na_1(obj)
        assert data[3:5] == b"\x00\x00"  # normalized zero

    def test_decode(self) -> None:
        # IOA=100 + 0x4000 (=0.5) + QDS=0
        data = b"\x64\x00\x00\x00\x40\x00"
        obj, off = decode_m_me_na_1(data, 0)
        assert obj.ioa == 100
        assert abs(obj.value - 0.5) < 0.0001
        assert off == 6

    def test_roundtrip(self) -> None:
        obj = MeasuredValueNormalized(ioa=500, value=-0.75, quality=QualityFlag.IV)
        result = decode_m_me_na_1(encode_m_me_na_1(obj), 0)[0]
        assert result.ioa == obj.ioa
        assert abs(result.value - obj.value) < 0.0001
        assert result.quality == obj.quality


# ==========================================================================
# M_ME_NB_1
# ==========================================================================


class TestM_ME_NB_1:
    def test_encode(self) -> None:
        obj = MeasuredValueScaled(ioa=100, value=12345)
        data = encode_m_me_nb_1(obj)
        assert len(data) == 6

    def test_decode(self) -> None:
        # IOA=100 + int16(12345=0x3039) + QDS=0
        data = b"\x64\x00\x00\x39\x30\x00"
        obj, off = decode_m_me_nb_1(data, 0)
        assert obj.ioa == 100
        assert obj.value == 12345
        assert off == 6

    def test_negative_value(self) -> None:
        obj = MeasuredValueScaled(ioa=100, value=-32768)
        data = encode_m_me_nb_1(obj)
        result = decode_m_me_nb_1(data, 0)[0]
        assert result.value == -32768

    def test_roundtrip(self) -> None:
        obj = MeasuredValueScaled(ioa=1000, value=32767, quality=QualityFlag.OV)
        result = decode_m_me_nb_1(encode_m_me_nb_1(obj), 0)[0]
        assert result == obj


# ==========================================================================
# M_ME_NC_1
# ==========================================================================


class TestM_ME_NC_1:
    def test_encode(self) -> None:
        obj = MeasuredValueShort(ioa=100, value=3.14)
        data = encode_m_me_nc_1(obj)
        assert len(data) == 8

    def test_decode(self) -> None:
        import struct

        val_bytes = struct.pack("<f", 42.5)
        data = b"\x64\x00\x00" + val_bytes + b"\x00"
        obj, off = decode_m_me_nc_1(data, 0)
        assert obj.ioa == 100
        assert abs(obj.value - 42.5) < 0.001
        assert off == 8

    def test_roundtrip(self) -> None:
        obj = MeasuredValueShort(ioa=3000, value=-273.15, quality=QualityFlag.IV | QualityFlag.NT)
        result = decode_m_me_nc_1(encode_m_me_nc_1(obj), 0)[0]
        assert result.ioa == obj.ioa
        assert abs(result.value - obj.value) < 0.0001
        assert result.quality == obj.quality


# ==========================================================================
# M_SP_TB_1
# ==========================================================================


class TestM_SP_TB_1:
    def test_encode(self) -> None:
        obj = SinglePointWithTime(ioa=100, value=True, timestamp=_TS)
        data = encode_m_sp_tb_1(obj)
        assert len(data) == 11

    def test_decode(self) -> None:
        ts_bytes = b"\x00\x00\x00\x00\xc1\x01\x00"
        data = b"\x64\x00\x00\x01" + ts_bytes
        obj, off = decode_m_sp_tb_1(data, 0)
        assert obj.ioa == 100
        assert obj.value is True
        assert obj.timestamp is not None
        assert obj.timestamp.day == 1
        assert off == 11

    def test_roundtrip(self) -> None:
        ts = CP56Time2a(
            milliseconds=12345,
            minutes=30,
            hours=12,
            day=15,
            month=9,
            year=2026,
        )
        obj = SinglePointWithTime(ioa=100, value=False, quality=QualityFlag.SB, timestamp=ts)
        result = decode_m_sp_tb_1(encode_m_sp_tb_1(obj), 0)[0]
        assert result == obj


# ==========================================================================
# M_DP_TB_1
# ==========================================================================


class TestM_DP_TB_1:
    def test_roundtrip(self) -> None:
        obj = DoublePointWithTime(ioa=200, value=1, quality=QualityFlag.BL, timestamp=_TS)
        result = decode_m_dp_tb_1(encode_m_dp_tb_1(obj), 0)[0]
        assert result == obj


# ==========================================================================
# M_ME_TD_1
# ==========================================================================


class TestM_ME_TD_1:
    def test_roundtrip(self) -> None:
        obj = MeasuredValueNormalizedWithTime(
            ioa=300,
            value=0.75,
            quality=QualityFlag.OV,
            timestamp=_TS,
        )
        result = decode_m_me_td_1(encode_m_me_td_1(obj), 0)[0]
        assert result.ioa == obj.ioa
        assert abs(result.value - obj.value) < 0.0001


# ==========================================================================
# M_ME_TF_1
# ==========================================================================


class TestM_ME_TF_1:
    def test_encode(self) -> None:
        obj = MeasuredValueShortWithTime(ioa=100, value=9.81, timestamp=_TS)
        data = encode_m_me_tf_1(obj)
        assert len(data) == 15

    def test_decode(self) -> None:
        import struct

        val_bytes = struct.pack("<f", 220.0)
        ts_bytes = b"\x00\x00\x00\x00\xc1\x01\x00"
        data = b"\x64\x00\x00" + val_bytes + b"\x00" + ts_bytes
        obj, off = decode_m_me_tf_1(data, 0)
        assert obj.ioa == 100
        assert abs(obj.value - 220.0) < 0.01
        assert off == 15

    def test_roundtrip(self) -> None:
        ts = CP56Time2a(
            milliseconds=50000,
            minutes=45,
            hours=18,
            day=31,
            month=12,
            year=2030,
        )
        obj = MeasuredValueShortWithTime(
            ioa=0xABCDEF,
            value=1e6,
            quality=QualityFlag.IV | QualityFlag.OV,
            timestamp=ts,
        )
        result = decode_m_me_tf_1(encode_m_me_tf_1(obj), 0)[0]
        assert result == obj


# ==========================================================================
# C_SC_NA_1
# ==========================================================================


class TestC_SC_NA_1:
    def test_encode_execute_on(self) -> None:
        obj = SingleCommand(ioa=100, value=True, select=False)
        data = encode_c_sc_na_1(obj)
        assert data[3] == 0x01  # SCO: ON, execute

    def test_encode_select_off(self) -> None:
        obj = SingleCommand(ioa=100, value=False, select=True)
        data = encode_c_sc_na_1(obj)
        assert data[3] == 0x80  # SCO: OFF, select (bit 7 set)

    def test_decode(self) -> None:
        data = b"\x64\x00\x00\x81"  # IOA=100, SCO=ON+select
        obj, off = decode_c_sc_na_1(data, 0)
        assert obj.ioa == 100
        assert obj.value is True
        assert obj.select is True

    def test_roundtrip(self) -> None:
        obj = SingleCommand(ioa=5555, value=False, select=True)
        result = decode_c_sc_na_1(encode_c_sc_na_1(obj), 0)[0]
        assert result == obj


# ==========================================================================
# C_DC_NA_1
# ==========================================================================


class TestC_DC_NA_1:
    def test_encode(self) -> None:
        obj = DoubleCommand(ioa=100, value=2, select=False)
        data = encode_c_dc_na_1(obj)
        assert data[3] == 0x02

    def test_decode(self) -> None:
        data = b"\x64\x00\x00\x82"  # IOA=100, DCO=ON+select
        obj, off = decode_c_dc_na_1(data, 0)
        assert obj.ioa == 100
        assert obj.value == 2
        assert obj.select is True

    def test_roundtrip(self) -> None:
        obj = DoubleCommand(ioa=7777, value=1, select=False)
        result = decode_c_dc_na_1(encode_c_dc_na_1(obj), 0)[0]
        assert result == obj


# ==========================================================================
# C_SE_NC_1
# ==========================================================================


class TestC_SE_NC_1:
    def test_encode(self) -> None:
        obj = SetpointCommandShort(ioa=100, value=50.0, select=False)
        data = encode_c_se_nc_1(obj)
        assert len(data) == 8

    def test_decode(self) -> None:
        import struct

        val_bytes = struct.pack("<f", 100.0)
        data = b"\x64\x00\x00" + val_bytes + b"\x80"
        obj, off = decode_c_se_nc_1(data, 0)
        assert obj.ioa == 100
        assert abs(obj.value - 100.0) < 0.001
        assert obj.select is True

    def test_roundtrip(self) -> None:
        obj = SetpointCommandShort(ioa=9999, value=-50.5, select=True)
        result = decode_c_se_nc_1(encode_c_se_nc_1(obj), 0)[0]
        assert result.ioa == obj.ioa
        assert abs(result.value - obj.value) < 0.001
        assert result.select == obj.select


# ==========================================================================
# C_IC_NA_1
# ==========================================================================


class TestC_IC_NA_1:
    def test_encode(self) -> None:
        obj = InterrogationCommand(ioa=0)
        data = encode_c_ic_na_1(obj)
        assert len(data) == 4
        assert data[3] == 0x14  # QOI=20

    def test_decode(self) -> None:
        data = b"\x00\x00\x00\x14"  # IOA=0, QOI=20
        obj, off = decode_c_ic_na_1(data, 0)
        assert obj.ioa == 0
        assert off == 4

    def test_roundtrip(self) -> None:
        obj = InterrogationCommand(ioa=0)
        result = decode_c_ic_na_1(encode_c_ic_na_1(obj), 0)[0]
        assert result == obj


# ==========================================================================
# C_CI_NA_1
# ==========================================================================


class TestC_CI_NA_1:
    def test_encode(self) -> None:
        obj = CounterInterrogationCommand(ioa=0)
        data = encode_c_ci_na_1(obj)
        assert len(data) == 4
        assert data[3] == 0x05  # QCC=5

    def test_decode(self) -> None:
        data = b"\x00\x00\x00\x05"  # IOA=0, QCC=5
        obj, off = decode_c_ci_na_1(data, 0)
        assert obj.ioa == 0
        assert off == 4

    def test_roundtrip(self) -> None:
        obj = CounterInterrogationCommand(ioa=0)
        result = decode_c_ci_na_1(encode_c_ci_na_1(obj), 0)[0]
        assert result == obj
