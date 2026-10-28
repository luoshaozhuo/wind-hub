"""IEC104 codec 纯单元测试（直接构造 hat 数据对象，无网络）。

- decode_quality / decode_data：九种监测数据类型与品质位；
- matches_monitoring_type：类型门一致/短格式/未知类型；
- decode_timestamp：无时间/无效/夏令时拒绝与有效 CP56Time2a；
- build_command：五种控制类型的类型与范围校验。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from hat.drivers.iec101 import common as iec101
from hat.drivers.iec60870.encodings import common as enc

from core.application.errors import ConfigError
from core.application.protocol_contract import Quality
from core.infrastructure.protocol.iec104.codec import (
    build_command,
    decode_data,
    decode_quality,
    decode_sample,
    decode_timestamp,
    matches_monitoring_type,
)
from core.infrastructure.protocol.iec104.mapping import IEC104Point

_IND = iec101.IndicationQuality(invalid=False, not_topical=False, substituted=False, blocked=False)
_MEAS = iec101.MeasurementQuality(
    invalid=False, not_topical=False, substituted=False, blocked=False, overflow=False
)


def _msg(data, *, time=None, cause=iec101.DataResCause.PERIODIC) -> iec101.DataMsg:
    return iec101.DataMsg(
        is_test=False,
        originator_address=0,
        asdu_address=1,
        io_address=10,
        data=data,
        time=time,
        cause=cause,
    )


# --------------------------------------------------------------------------
# decode_quality
# --------------------------------------------------------------------------


def test_quality_mapping() -> None:
    assert decode_quality(None) is Quality.UNCERTAIN
    assert decode_quality(iec101.IndicationQuality(True, False, False, False)) is Quality.BAD
    assert decode_quality(iec101.IndicationQuality(False, True, False, False)) is Quality.UNCERTAIN
    assert (
        decode_quality(iec101.MeasurementQuality(False, False, False, False, True))
        is Quality.UNCERTAIN
    )
    assert decode_quality(_IND) is Quality.GOOD


# --------------------------------------------------------------------------
# decode_data
# --------------------------------------------------------------------------


def test_decode_all_monitoring_types() -> None:
    assert decode_data(iec101.SingleData(iec101.SingleValue.ON, _IND)) == (True, Quality.GOOD)
    assert decode_data(iec101.SingleData(iec101.SingleValue.OFF, _IND))[0] is False

    value, _ = decode_data(iec101.DoubleData(iec101.DoubleValue.ON, _IND))
    assert value == int(iec101.DoubleValue.ON.value)

    value, quality = decode_data(iec101.StepPositionData(iec101.StepPositionValue(2, True), _MEAS))
    assert value == 2
    assert quality is Quality.UNCERTAIN  # transient 降级

    value, _ = decode_data(iec101.BitstringData(iec101.BitstringValue(bytes([1, 0, 1])), _MEAS))
    assert value == int.from_bytes(bytes([1, 0, 1]), "little")

    assert decode_data(iec101.NormalizedData(iec101.NormalizedValue(0.5), _MEAS))[0] == 0.5
    assert decode_data(iec101.ScaledData(iec101.ScaledValue(-100), _MEAS))[0] == -100
    assert decode_data(iec101.FloatingData(iec101.FloatingValue(3.25), _MEAS))[0] == 3.25
    assert (
        decode_data(
            iec101.BinaryCounterData(
                iec101.BinaryCounterValue(42),
                iec101.CounterQuality(False, False, False, 0),
            )
        )[0]
        == 42
    )

    bits = [True, False, True, False, True]
    value, _ = decode_data(iec101.StatusData(iec101.StatusValue(bits, [False] * 5), _MEAS))
    assert value == 0b10101


def test_decode_bad_quality_propagates() -> None:
    bad = iec101.IndicationQuality(True, False, False, False)
    _, quality = decode_data(iec101.SingleData(iec101.SingleValue.ON, bad))
    assert quality is Quality.BAD


def test_decode_unsupported_type_raises() -> None:
    with pytest.raises(ValueError, match="unsupported IEC104 monitoring data type"):
        decode_data(object())


# --------------------------------------------------------------------------
# matches_monitoring_type
# --------------------------------------------------------------------------


def test_matches_none_type_id_always() -> None:
    assert matches_monitoring_type(None, _msg(iec101.SingleData(iec101.SingleValue.ON, _IND)))


def test_matches_timed_and_untimed_names() -> None:
    msg = _msg(iec101.SingleData(iec101.SingleValue.ON, _IND))
    assert matches_monitoring_type("M_SP_NA_1", msg)
    assert not matches_monitoring_type("M_DP_NA_1", msg)


def test_matches_normalized_short_format() -> None:
    normalized = iec101.NormalizedData(iec101.NormalizedValue(0.1), _MEAS)
    assert matches_monitoring_type("M_ME_ND_1", _msg(normalized))


def test_matches_status_requires_untimed() -> None:
    status = iec101.StatusData(iec101.StatusValue([True], [False]), _MEAS)
    assert matches_monitoring_type("M_PS_NA_1", _msg(status))
    assert not matches_monitoring_type("M_PS_NA_1", _msg(status, time=object()))


def test_matches_unknown_data_type_false() -> None:
    assert not matches_monitoring_type("M_SP_NA_1", _msg(object()))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# decode_timestamp
# --------------------------------------------------------------------------


def test_timestamp_missing_or_invalid_falls_back_to_local() -> None:
    receive = datetime(2026, 10, 1, tzinfo=UTC)
    timestamp, source = decode_timestamp(
        _msg(iec101.SingleData(iec101.SingleValue.ON, _IND)),
        receive_time=receive,
        device_tz=ZoneInfo("Europe/Berlin"),
    )
    assert source == "local"
    assert timestamp == receive


def test_timestamp_rejects_summer_flag_on_winter_date() -> None:
    receive = datetime(2026, 1, 15, 12, tzinfo=UTC)
    time = enc.Time(
        size=enc.TimeSize.SEVEN,
        milliseconds=0,
        invalid=False,
        substituted=False,
        minutes=0,
        summer_time=True,  # 一月不可能处于夏令时
        hours=12,
        day_of_week=None,
        day_of_month=15,
        months=1,
        years=26,
    )
    timestamp, source = decode_timestamp(
        _msg(iec101.SingleData(iec101.SingleValue.ON, _IND), time=time),
        receive_time=receive,
        device_tz=ZoneInfo("Europe/Berlin"),
    )
    assert source == "local"
    assert timestamp == receive


def test_timestamp_valid_device_clock() -> None:
    receive = datetime(2026, 7, 1, 12, tzinfo=UTC)
    time = enc.Time(
        size=enc.TimeSize.SEVEN,
        milliseconds=0,
        invalid=False,
        substituted=False,
        minutes=30,
        summer_time=True,
        hours=14,
        day_of_week=None,
        day_of_month=1,
        months=7,
        years=26,
    )
    timestamp, source = decode_timestamp(
        _msg(iec101.SingleData(iec101.SingleValue.ON, _IND), time=time),
        receive_time=receive,
        device_tz=ZoneInfo("Europe/Berlin"),
    )
    assert source == "device"
    # 柏林夏令时 UTC+2：14:30 本地 = 12:30 UTC。
    assert timestamp == datetime(2026, 7, 1, 12, 30, tzinfo=UTC)


# --------------------------------------------------------------------------
# decode_sample
# --------------------------------------------------------------------------


def test_decode_sample_metadata() -> None:
    sample = decode_sample("wind_speed", _msg(iec101.SingleData(iec101.SingleValue.ON, _IND)))
    assert sample.point_id == "wind_speed"
    assert sample.value is True
    metadata = dict(sample.protocol_metadata)
    assert metadata["ca"] == 1
    assert metadata["ioa"] == 10
    assert metadata["cot"] == 1
    assert sample.timestamp_source == "local"
    assert sample.receive_time.tzinfo is not None


# --------------------------------------------------------------------------
# build_command
# --------------------------------------------------------------------------


def _mapped(type_id: str | None) -> IEC104Point:
    return IEC104Point(point_id="p", common_address=1, ioa=10, type_id=type_id)


def test_single_command_requires_bool() -> None:
    command = build_command(_mapped("C_SC_NA_1"), True)
    assert command.value is iec101.SingleValue.ON
    with pytest.raises(TypeError, match="boolean"):
        build_command(_mapped("C_SC_NA_1"), 1)


def test_double_command_range() -> None:
    command = build_command(_mapped("C_DC_NA_1"), 2.0)
    assert command.value is iec101.DoubleValue.ON
    with pytest.raises(ValueError, match="1 .OFF. or 2 .ON."):
        build_command(_mapped("C_DC_NA_1"), 3)
    with pytest.raises(ValueError, match="1 .OFF. or 2 .ON."):
        build_command(_mapped("C_DC_NA_1"), 1.5)
    with pytest.raises(TypeError):
        build_command(_mapped("C_DC_NA_1"), True)


def test_normalized_command_bounds_and_type() -> None:
    build_command(_mapped("C_SE_NA_1"), -1.0)
    build_command(_mapped("C_SE_NA_1"), 1.0)
    with pytest.raises(ValueError, match="-1..1"):
        build_command(_mapped("C_SE_NA_1"), 1.01)
    with pytest.raises(TypeError, match="numeric"):
        build_command(_mapped("C_SE_NA_1"), "0.5")
    with pytest.raises(ValueError, match="finite"):
        build_command(_mapped("C_SE_NA_1"), float("nan"))


def test_scaled_command_int16_range() -> None:
    build_command(_mapped("C_SE_NB_1"), -32768)
    with pytest.raises(ValueError, match="int16 range"):
        build_command(_mapped("C_SE_NB_1"), 32768)
    with pytest.raises(ValueError, match="integer value"):
        build_command(_mapped("C_SE_NB_1"), 1.5)


def test_floating_command() -> None:
    command = build_command(_mapped("C_SE_NC_1"), 3.25)
    assert command.value.value == pytest.approx(3.25)
    with pytest.raises(TypeError):
        build_command(_mapped("C_SE_NC_1"), True)
    with pytest.raises(ValueError, match="finite"):
        build_command(_mapped("C_SE_NC_1"), float("inf"))


def test_command_requires_supported_type_id() -> None:
    with pytest.raises(ConfigError, match="requires type_id"):
        build_command(_mapped(None), 1)
    with pytest.raises(ConfigError, match="unsupported command type"):
        build_command(_mapped("M_SP_NA_1"), 1)
