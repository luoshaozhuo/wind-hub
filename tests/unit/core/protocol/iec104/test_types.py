"""Tests for IEC104 type enumerations."""

from __future__ import annotations

from wind_hub_core.protocol.iec104.codec.types import (
    CauseOfTransmission,
    QualityFlag,
    TypeID,
    UFrameType,
)


class TestTypeID:
    def test_values(self) -> None:
        assert TypeID.M_SP_NA_1 == 1
        assert TypeID.M_DP_NA_1 == 3
        assert TypeID.M_ME_NA_1 == 9
        assert TypeID.M_ME_NB_1 == 11
        assert TypeID.M_ME_NC_1 == 13
        assert TypeID.M_SP_TB_1 == 30
        assert TypeID.M_DP_TB_1 == 31
        assert TypeID.M_ME_TD_1 == 34
        assert TypeID.M_ME_TF_1 == 36
        assert TypeID.C_SC_NA_1 == 45
        assert TypeID.C_DC_NA_1 == 46
        assert TypeID.C_SE_NC_1 == 50
        assert TypeID.C_IC_NA_1 == 100
        assert TypeID.C_CI_NA_1 == 103

    def test_from_int(self) -> None:
        assert TypeID(1) == TypeID.M_SP_NA_1
        assert TypeID(100) == TypeID.C_IC_NA_1


class TestCauseOfTransmission:
    def test_values(self) -> None:
        assert CauseOfTransmission.SPONTANEOUS == 3
        assert CauseOfTransmission.REQUEST == 5
        assert CauseOfTransmission.ACTIVATION == 6
        assert CauseOfTransmission.ACTIVATION_CON == 7
        assert CauseOfTransmission.DEACTIVATION == 8
        assert CauseOfTransmission.DEACTIVATION_CON == 9
        assert CauseOfTransmission.ACTIVATION_TERMINATION == 10
        assert CauseOfTransmission.INTERROGATED_BY_STATION == 20
        assert CauseOfTransmission.UNKNOWN_TYPE == 44
        assert CauseOfTransmission.UNKNOWN_COT == 45
        assert CauseOfTransmission.UNKNOWN_CA == 46
        assert CauseOfTransmission.UNKNOWN_IOA == 47

    def test_from_int(self) -> None:
        assert CauseOfTransmission(3) == CauseOfTransmission.SPONTANEOUS


class TestUFrameType:
    def test_values(self) -> None:
        assert UFrameType.STARTDT_ACT == 0x07
        assert UFrameType.STARTDT_CON == 0x0B
        assert UFrameType.STOPDT_ACT == 0x13
        assert UFrameType.STOPDT_CON == 0x23
        assert UFrameType.TESTFR_ACT == 0x43
        assert UFrameType.TESTFR_CON == 0x83

    def test_from_int(self) -> None:
        assert UFrameType(0x07) == UFrameType.STARTDT_ACT


class TestQualityFlag:
    def test_bit_values(self) -> None:
        assert QualityFlag.OV == 0x01
        assert QualityFlag.BL == 0x02
        assert QualityFlag.SB == 0x04
        assert QualityFlag.NT == 0x08
        assert QualityFlag.IV == 0x10

    def test_bitwise_or(self) -> None:
        flags = QualityFlag.IV | QualityFlag.OV
        assert flags == 0x11
        assert QualityFlag.IV in flags
        assert QualityFlag.OV in flags
        assert QualityFlag.NT not in flags

    def test_none_flags(self) -> None:
        assert QualityFlag(0) == 0
        assert not QualityFlag(0)  # zero value is falsy
