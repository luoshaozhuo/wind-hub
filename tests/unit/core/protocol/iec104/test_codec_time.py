"""hat 报文 ↔ ProtocolSample/命令 的 codec 单元测试。

覆盖：CP56Time2a 时标（IANA 时区、夏令时、世纪翻转、歧义/缺失回退）、
品质位归一、全部受支持监测 IO 元素解码、命令构造的严格类型/范围校验，
以及主动读响应的 Type ID 匹配。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hat.drivers.iec101 import common as iec101

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

BERLIN = ZoneInfo("Europe/Berlin")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def good_mq() -> iec101.MeasurementQuality:
    return iec101.MeasurementQuality(
        invalid=False,
        not_topical=False,
        substituted=False,
        blocked=False,
        overflow=False,
    )


def good_iq() -> iec101.IndicationQuality:
    return iec101.IndicationQuality(
        invalid=False, not_topical=False, substituted=False, blocked=False
    )


def cp56(
    year: int | None,
    month: int | None,
    day: int | None,
    hour: int | None,
    minute: int | None,
    second: int = 0,
    millisecond: int = 0,
    *,
    invalid: bool | None = False,
    summer: bool | None = False,
    size: iec101.TimeSize = iec101.TimeSize.SEVEN,
) -> iec101.Time:
    return iec101.Time(
        size=size,
        milliseconds=second * 1000 + millisecond,
        invalid=invalid,
        substituted=None,
        minutes=minute,
        summer_time=summer,
        hours=hour,
        day_of_week=None,
        day_of_month=day,
        months=month,
        years=year,
    )


def data_msg(
    data: iec101.Data,
    *,
    time: object = None,
    cause: object = iec101.DataResCause.SPONTANEOUS,
    ca: int = 1,
    ioa: int = 100,
) -> iec101.DataMsg:
    return iec101.DataMsg(
        is_test=False,
        originator_address=1,
        asdu_address=ca,
        io_address=ioa,
        data=data,
        time=time,
        cause=cause,
    )


def floating_msg(
    value: float = 1.0,
    *,
    time: object = None,
    quality: iec101.MeasurementQuality | None = None,
) -> iec101.DataMsg:
    return data_msg(
        iec101.FloatingData(iec101.FloatingValue(value), quality or good_mq()),
        time=time,
    )


def cmd_point(type_id: str | None) -> IEC104Point:
    return IEC104Point(point_id="cmd", common_address=1, ioa=1001, type_id=type_id)


# ---------------------------------------------------------------------------
# 品质位
# ---------------------------------------------------------------------------


def test_clean_measurement_quality_is_good() -> None:
    assert decode_quality(good_mq()) is Quality.GOOD


def test_clean_indication_quality_is_good() -> None:
    assert decode_quality(good_iq()) is Quality.GOOD


def test_invalid_quality_is_bad() -> None:
    assert (
        decode_quality(iec101.MeasurementQuality(True, False, False, False, False)) is Quality.BAD
    )
    assert decode_quality(iec101.IndicationQuality(True, False, False, False)) is Quality.BAD


@pytest.mark.parametrize(
    "field",
    ["not_topical", "substituted", "blocked", "overflow"],
)
def test_measurement_uncertain_flags(field: str) -> None:
    kwargs = {
        name: name == field
        for name in ("invalid", "not_topical", "substituted", "blocked", "overflow")
    }
    assert decode_quality(iec101.MeasurementQuality(**kwargs)) is Quality.UNCERTAIN


def test_counter_quality_adjusted_and_overflow_are_uncertain() -> None:
    assert decode_quality(iec101.CounterQuality(False, True, False, 0)) is Quality.UNCERTAIN
    assert decode_quality(iec101.CounterQuality(False, False, True, 0)) is Quality.UNCERTAIN


def test_invalid_dominates_other_quality_flags() -> None:
    assert decode_quality(iec101.MeasurementQuality(True, True, True, True, True)) is Quality.BAD


def test_absent_quality_is_uncertain_never_good() -> None:
    assert decode_quality(None) is Quality.UNCERTAIN


# ---------------------------------------------------------------------------
# CP56Time2a 时标
# ---------------------------------------------------------------------------


def test_valid_device_time_uses_device_source_and_keeps_independent_receive_time() -> None:
    receive_time = datetime(2026, 1, 15, 2, 30, 15, tzinfo=UTC)
    # 设备本地 10:30:15.250 上海时间 = 02:30:15.250 UTC。
    device = cp56(26, 1, 15, 10, 30, 15, 250, summer=False)

    timestamp, source = decode_timestamp(
        floating_msg(time=device), receive_time=receive_time, device_tz=SHANGHAI
    )

    assert source == "device"
    assert timestamp == datetime(2026, 1, 15, 2, 30, 15, 250000, tzinfo=UTC)
    sample = decode_sample(
        "p",
        floating_msg(time=device),
        receive_time=receive_time,
        device_tz=SHANGHAI,
    )
    assert sample.timestamp_source == "device"
    assert sample.device_time == sample.timestamp
    assert sample.receive_time == receive_time
    assert sample.receive_time != sample.timestamp


def test_iana_timezones_convert_to_utc_with_and_without_dst() -> None:
    # 上海无夏令时：UTC+8。
    ts, source = decode_timestamp(
        floating_msg(time=cp56(26, 7, 15, 12, 0, summer=False)),
        receive_time=datetime.now(UTC),
        device_tz=SHANGHAI,
    )
    assert source == "device"
    assert ts == datetime(2026, 7, 15, 4, 0, tzinfo=UTC)

    # 柏林冬季 CET（UTC+1）。
    ts_winter, _ = decode_timestamp(
        floating_msg(time=cp56(26, 1, 15, 10, 30, summer=False)),
        receive_time=datetime.now(UTC),
        device_tz=BERLIN,
    )
    assert ts_winter == datetime(2026, 1, 15, 9, 30, tzinfo=UTC)

    # 柏林夏季 CEST（UTC+2），同一本地时刻换算结果不同。
    ts_summer, source = decode_timestamp(
        floating_msg(time=cp56(26, 7, 15, 10, 30, summer=True)),
        receive_time=datetime.now(UTC),
        device_tz=BERLIN,
    )
    assert source == "device"
    assert ts_summer == datetime(2026, 7, 15, 8, 30, tzinfo=UTC)


def test_wrong_summer_time_flag_falls_back_to_receive_time() -> None:
    receive_time = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
    # 1 月柏林无 DST，却声称 summer_time=True。
    _, source = decode_timestamp(
        floating_msg(time=cp56(26, 1, 15, 10, 30, summer=True)),
        receive_time=receive_time,
        device_tz=BERLIN,
    )
    assert source == "local"


def test_ambiguous_fold_time_is_disambiguated_by_summer_flag() -> None:
    # 2026-10-25 02:30 在柏林出现两次：02:30 CEST（fold=0）与 02:30 CET（fold=1）。
    # summer_time 标志必须选出唯一解释，而不是猜测 fold。
    base = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
    ts_summer, source_summer = decode_timestamp(
        floating_msg(time=cp56(26, 10, 25, 2, 30, summer=True)),
        receive_time=base,
        device_tz=BERLIN,
    )
    assert source_summer == "device"
    assert ts_summer == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)

    ts_winter, source_winter = decode_timestamp(
        floating_msg(time=cp56(26, 10, 25, 2, 30, summer=False)),
        receive_time=base,
        device_tz=BERLIN,
    )
    assert source_winter == "device"
    assert ts_winter == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_nonexistent_local_time_falls_back_to_receive_time() -> None:
    receive_time = datetime(2026, 3, 29, 3, 0, tzinfo=UTC)
    # 02:30 落在柏林春令时跳变空档（02:00 -> 03:00），无论 SU 标志如何都不存在。
    for summer in (True, False):
        timestamp, source = decode_timestamp(
            floating_msg(time=cp56(26, 3, 29, 2, 30, summer=summer)),
            receive_time=receive_time,
            device_tz=BERLIN,
        )
        assert source == "local"
        assert timestamp == receive_time


@pytest.mark.parametrize(
    ("time_kwargs", "mutate"),
    [
        ({}, "invalid"),
        ({}, "summer_none"),
        ({}, "short_size"),
        ({}, "missing_year"),
        ({}, "missing_month"),
    ],
)
def test_malformed_cp56_falls_back_to_receive_time(
    time_kwargs: dict[str, object], mutate: str
) -> None:
    receive_time = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    time = cp56(26, 1, 15, 10, 30, summer=False, **time_kwargs)
    if mutate == "invalid":
        time = cp56(26, 1, 15, 10, 30, invalid=True)
    elif mutate == "summer_none":
        time = cp56(26, 1, 15, 10, 30, summer=None)
    elif mutate == "short_size":
        time = cp56(26, 1, 15, 10, 30, size=iec101.TimeSize.THREE)
    elif mutate == "missing_year":
        time = cp56(None, 1, 15, 10, 30)
    elif mutate == "missing_month":
        time = cp56(26, None, 15, 10, 30)

    timestamp, source = decode_timestamp(
        floating_msg(time=time), receive_time=receive_time, device_tz=BERLIN
    )
    assert source == "local"
    assert timestamp == receive_time


def test_missing_time_field_falls_back() -> None:
    receive_time = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    timestamp, source = decode_timestamp(
        floating_msg(time=None), receive_time=receive_time, device_tz=BERLIN
    )
    assert source == "local"
    assert timestamp == receive_time


def test_no_configured_timezone_falls_back_to_local() -> None:
    receive_time = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
    timestamp, source = decode_timestamp(
        floating_msg(time=cp56(26, 1, 15, 10, 30)),
        receive_time=receive_time,
        device_tz=None,
    )
    assert source == "local"
    assert timestamp == receive_time


def test_default_receive_time_is_recent_utc() -> None:
    timestamp, source = decode_timestamp(
        floating_msg(), receive_time=datetime.now(UTC), device_tz=None
    )
    assert source == "local"
    assert timestamp.tzinfo is UTC
    assert datetime.now(UTC) - timestamp < timedelta(seconds=5)


def test_century_rollover_boundary_at_70() -> None:
    # year<70 → 2000+year；year>=70 → 1900+year。
    receive_time = datetime(2000, 1, 1, tzinfo=UTC)

    ts_modern, source = decode_timestamp(
        floating_msg(time=cp56(69, 1, 1, 1, 0, summer=False)),
        receive_time=receive_time,
        device_tz=BERLIN,
    )
    assert source == "device"
    # 2069-01-01 01:00 CET（UTC+1）。
    assert ts_modern == datetime(2069, 1, 1, 0, 0, tzinfo=UTC)

    ts_legacy, source = decode_timestamp(
        floating_msg(time=cp56(70, 1, 1, 1, 0, summer=False)),
        receive_time=receive_time,
        device_tz=BERLIN,
    )
    assert source == "device"
    # 1970-01-01 01:00 CET（UTC+1）。
    assert ts_legacy == datetime(1970, 1, 1, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 监测数据解码
# ---------------------------------------------------------------------------


def test_single_and_double_data() -> None:
    value, quality = decode_data(iec101.SingleData(iec101.SingleValue.ON, good_iq()))
    assert value is True and quality is Quality.GOOD
    value, _ = decode_data(iec101.SingleData(iec101.SingleValue.OFF, good_iq()))
    assert value is False

    value, quality = decode_data(iec101.DoubleData(iec101.DoubleValue.ON, good_iq()))
    assert value == 2 and quality is Quality.GOOD
    value, _ = decode_data(iec101.DoubleData(iec101.DoubleValue.FAULT, good_iq()))
    assert value == 3


def test_step_position_transient_is_uncertain() -> None:
    value, quality = decode_data(
        iec101.StepPositionData(iec101.StepPositionValue(3, transient=False), good_mq())
    )
    assert value == 3 and quality is Quality.GOOD

    value, quality = decode_data(
        iec101.StepPositionData(iec101.StepPositionValue(3, transient=True), good_mq())
    )
    assert value == 3 and quality is Quality.UNCERTAIN


def test_bitstring_data_decoded_as_unsigned_int() -> None:
    # 复现生产缺陷：codec.decode_data 对 M_BO_NA_1 执行 int(bytes) 必然抛
    # ValueError（src/core/infrastructure/protocol/iec104/codec.py:168），
    # 导致 matches_monitoring_type 已声明支持的 M_BO_NA_1/M_BO_TB_1 永远无法
    # 产出样本（主动读失败、自发数据被丢弃）。正确行为应为按无符号 32 位整数解码。
    value, quality = decode_data(
        iec101.BitstringData(iec101.BitstringValue(b"\x01\x00\x00\x00"), good_mq())
    )
    assert value == 1 and quality is Quality.GOOD


def test_numeric_measurement_data() -> None:
    value, quality = decode_data(iec101.NormalizedData(iec101.NormalizedValue(0.5), good_mq()))
    assert value == pytest.approx(0.5) and quality is Quality.GOOD

    value, _ = decode_data(iec101.ScaledData(iec101.ScaledValue(-1234), good_mq()))
    assert value == -1234 and isinstance(value, int)

    value, _ = decode_data(iec101.FloatingData(iec101.FloatingValue(1500.5), good_mq()))
    assert value == pytest.approx(1500.5)


def test_binary_counter_data() -> None:
    value, quality = decode_data(
        iec101.BinaryCounterData(
            iec101.BinaryCounterValue(42),
            iec101.CounterQuality(False, False, False, 0),
        )
    )
    assert value == 42 and quality is Quality.GOOD


def test_status_data_bits_as_int() -> None:
    value, quality = decode_data(
        iec101.StatusData(
            iec101.StatusValue([True, False, True, False], [False, False, False, False]),
            good_mq(),
        )
    )
    assert value == 0b0101 and quality is Quality.GOOD


def test_data_without_quality_object_is_uncertain() -> None:
    _, quality = decode_data(iec101.NormalizedData(iec101.NormalizedValue(0.5), None))
    assert quality is Quality.UNCERTAIN


def test_unsupported_data_type_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported IEC104 monitoring data type"):
        decode_data(object())


def test_decode_sample_carries_protocol_metadata() -> None:
    sample = decode_sample(
        "windspeed",
        data_msg(
            iec101.FloatingData(iec101.FloatingValue(7.5), good_mq()),
            ca=7,
            ioa=2001,
            cause=iec101.DataResCause.REQUEST,
        ),
        device_tz=None,
    )
    assert sample.point_id == "windspeed"
    assert sample.value == pytest.approx(7.5)
    assert dict(sample.protocol_metadata) == {"ca": 7, "ioa": 2001, "cot": 5}


# ---------------------------------------------------------------------------
# 主动读 Type ID 匹配
# ---------------------------------------------------------------------------


def test_matches_monitoring_type() -> None:
    untimed = data_msg(iec101.FloatingData(iec101.FloatingValue(1.0), good_mq()), time=None)
    timed = data_msg(
        iec101.FloatingData(iec101.FloatingValue(1.0), good_mq()),
        time=cp56(26, 1, 15, 10, 30),
    )
    assert matches_monitoring_type("M_ME_NC_1", untimed)
    assert not matches_monitoring_type("M_ME_TF_1", untimed)
    assert matches_monitoring_type("M_ME_TF_1", timed)
    assert not matches_monitoring_type("M_ME_NC_1", timed)
    assert matches_monitoring_type(None, untimed)

    normalized_untimed = data_msg(
        iec101.NormalizedData(iec101.NormalizedValue(0.5), good_mq()), time=None
    )
    assert matches_monitoring_type("M_ME_ND_1", normalized_untimed)
    assert matches_monitoring_type("M_ME_NA_1", normalized_untimed)
    normalized_timed = data_msg(
        iec101.NormalizedData(iec101.NormalizedValue(0.5), good_mq()),
        time=cp56(26, 1, 15, 10, 30),
    )
    assert matches_monitoring_type("M_ME_TD_1", normalized_timed)
    assert not matches_monitoring_type("M_ME_NA_1", normalized_timed)

    status = data_msg(
        iec101.StatusData(iec101.StatusValue([False], [False]), good_mq()),
        time=None,
    )
    assert matches_monitoring_type("M_PS_NA_1", status)
    assert not matches_monitoring_type(
        "M_PS_NA_1",
        data_msg(
            iec101.StatusData(iec101.StatusValue([False], [False]), good_mq()),
            time=cp56(26, 1, 15, 10, 30),
        ),
    )


# ---------------------------------------------------------------------------
# 命令构造
# ---------------------------------------------------------------------------


def test_single_command_requires_bool() -> None:
    with pytest.raises(TypeError, match="boolean"):
        build_command(cmd_point("C_SC_NA_1"), 1)
    command = build_command(cmd_point("C_SC_NA_1"), False)
    assert isinstance(command, iec101.SingleCommand)
    assert command.value is iec101.SingleValue.OFF
    assert command.select is False
    assert command.qualifier == 0


def test_double_command_requires_1_or_2() -> None:
    with pytest.raises(TypeError, match="1 .OFF. or 2 .ON."):
        build_command(cmd_point("C_DC_NA_1"), True)
    with pytest.raises(ValueError, match="1 .OFF. or 2 .ON."):
        build_command(cmd_point("C_DC_NA_1"), 3)
    with pytest.raises(ValueError, match="1 .OFF. or 2 .ON."):
        build_command(cmd_point("C_DC_NA_1"), 1.5)
    assert build_command(cmd_point("C_DC_NA_1"), 1).value is iec101.DoubleValue.OFF
    assert build_command(cmd_point("C_DC_NA_1"), 2).value is iec101.DoubleValue.ON


def test_normalized_setpoint_range_and_type() -> None:
    with pytest.raises(ValueError, match=r"-1\.\.1"):
        build_command(cmd_point("C_SE_NA_1"), 1.5)
    with pytest.raises(TypeError, match="numeric"):
        build_command(cmd_point("C_SE_NA_1"), "0.5")
    command = build_command(cmd_point("C_SE_NA_1"), -1.0)
    assert isinstance(command, iec101.NormalizedCommand)
    assert command.value.value == -1.0
    assert build_command(cmd_point("C_SE_NA_1"), 1).value.value == 1.0


def test_scaled_setpoint_requires_int16() -> None:
    with pytest.raises(ValueError, match="int16"):
        build_command(cmd_point("C_SE_NB_1"), 32768)
    with pytest.raises(ValueError, match="int16"):
        build_command(cmd_point("C_SE_NB_1"), -32769)
    with pytest.raises(ValueError, match="integer"):
        build_command(cmd_point("C_SE_NB_1"), 1.5)
    with pytest.raises(TypeError, match="numeric"):
        build_command(cmd_point("C_SE_NB_1"), True)
    command = build_command(cmd_point("C_SE_NB_1"), 32767)
    assert isinstance(command, iec101.ScaledCommand)
    assert command.value.value == 32767


def test_floating_setpoint_rejects_non_finite() -> None:
    with pytest.raises(ValueError, match="finite"):
        build_command(cmd_point("C_SE_NC_1"), float("nan"))
    with pytest.raises(ValueError, match="finite"):
        build_command(cmd_point("C_SE_NC_1"), float("inf"))
    with pytest.raises(TypeError, match="numeric"):
        build_command(cmd_point("C_SE_NC_1"), True)
    command = build_command(cmd_point("C_SE_NC_1"), 42)
    assert isinstance(command, iec101.FloatingCommand)
    assert command.value.value == 42.0


def test_unknown_or_missing_command_type_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="unsupported command type"):
        build_command(cmd_point("C_RC_NA_1"), 1)
    with pytest.raises(ConfigError, match="requires type_id"):
        build_command(cmd_point(None), True)
