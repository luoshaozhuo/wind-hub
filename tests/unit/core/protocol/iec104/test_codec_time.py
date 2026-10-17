"""hat 报文 ↔ ProtocolSample/命令 的 codec 单元测试（品质、时标、类型）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hat.drivers.iec101 import common as iec101

from core.application.protocol_contract import Quality
from core.infrastructure.protocol.iec104.codec import (
    build_command,
    decode_data,
    decode_quality,
    decode_sample,
    decode_timestamp,
)
from core.infrastructure.protocol.iec104.mapping import iec104_point


def _floating_msg(
    value: float,
    *,
    quality: iec101.MeasurementQuality | None = None,
    time: object | None = None,
    cause: object = iec101.DataResCause.SPONTANEOUS,
    ca: int = 1,
    ioa: int = 100,
) -> iec101.DataMsg:
    return iec101.DataMsg(
        is_test=False,
        originator_address=1,
        asdu_address=ca,
        io_address=ioa,
        data=iec101.FloatingData(
            value=iec101.FloatingValue(value),
            quality=quality or iec101.MeasurementQuality(
                invalid=False,
                not_topical=False,
                substituted=False,
                blocked=False,
                overflow=False,
            ),
        ),
        time=time,
        cause=cause,
    )


# ---------------------------------------------------------------------------
# 品质
# ---------------------------------------------------------------------------


def test_clean_measurement_quality_is_good() -> None:
    quality = iec101.MeasurementQuality(False, False, False, False, False)
    assert decode_quality(quality) is Quality.GOOD


def test_invalid_quality_is_bad() -> None:
    quality = iec101.MeasurementQuality(True, False, False, False, False)
    assert decode_quality(quality) is Quality.BAD


@pytest.mark.parametrize(
    "field",
    ["not_topical", "substituted", "blocked", "overflow"],
)
def test_deprecated_quality_is_uncertain(field: str) -> None:
    kwargs = {name: name == field for name in (
        "invalid",
        "not_topical",
        "substituted",
        "blocked",
        "overflow",
    )}
    quality = iec101.MeasurementQuality(**kwargs)
    assert decode_quality(quality) is Quality.UNCERTAIN


def test_absent_quality_is_not_treated_as_good() -> None:
    assert decode_quality(None) is Quality.UNCERTAIN


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------


def test_missing_device_timestamp_gets_local_utc_time() -> None:
    timestamp, source = decode_timestamp(_floating_msg(1.0, time=None))
    assert source == "local"
    assert timestamp.tzinfo is UTC
    assert datetime.now(UTC) - timestamp < timedelta(seconds=5)


def test_device_cp56time2a_is_interpreted_in_host_local_time() -> None:
    # hat 按主机本地时区解释 CP56Time2a 字段再归一为 UTC。
    local_wall = datetime(2026, 1, 1, 8, 30, 15)
    device_time = iec101.time_from_datetime(
        local_wall.replace(tzinfo=datetime.now().astimezone().tzinfo)
    )
    msg = _floating_msg(1.0, time=device_time)

    timestamp, source = decode_timestamp(msg)
    assert source == "device"
    expected = local_wall.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(UTC)
    assert timestamp == expected


def test_invalid_time_flag_falls_back_to_local() -> None:
    device_time = iec101.Time(
        size=iec101.TimeSize.SEVEN,
        milliseconds=0,
        invalid=True,
        substituted=False,
        minutes=0,
        summer_time=False,
        hours=0,
        day_of_week=1,
        day_of_month=1,
        months=1,
        years=26,
    )
    timestamp, source = decode_timestamp(_floating_msg(1.0, time=device_time))
    assert source == "local"
    assert timestamp.tzinfo is UTC


# ---------------------------------------------------------------------------
# 值解码
# ---------------------------------------------------------------------------


def test_floating_sample_value_quality_and_identity() -> None:
    msg = _floating_msg(1500.5)
    sample = decode_sample("windspeed", msg)
    assert sample.point_id == "windspeed"
    assert sample.value == pytest.approx(1500.5, abs=1e-3)
    assert sample.quality is Quality.GOOD


def test_single_and_double_point_decoding() -> None:
    indication_good = iec101.IndicationQuality(False, False, False, False)
    on_msg = iec101.DataMsg(
        is_test=False,
        originator_address=1,
        asdu_address=1,
        io_address=1,
        data=iec101.SingleData(iec101.SingleValue.ON, indication_good),
        time=None,
        cause=iec101.DataResCause.SPONTANEOUS,
    )
    value, quality = decode_data(on_msg.data)
    assert value is True and quality is Quality.GOOD

    double_msg = iec101.DataMsg(
        is_test=False,
        originator_address=1,
        asdu_address=1,
        io_address=2,
        data=iec101.DoubleData(iec101.DoubleValue.ON, indication_good),
        time=None,
        cause=iec101.DataResCause.SPONTANEOUS,
    )
    value, _ = decode_data(double_msg.data)
    assert value == iec101.DoubleValue.ON.value


def test_unsupported_data_type_raises() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        decode_data(object())


# ---------------------------------------------------------------------------
# 命令构造：严格类型
# ---------------------------------------------------------------------------


def _cmd_point(type_id: str, ioa: int = 1001, ca: int = 1):
    return iec104_point("cmd", ioa=ioa, common_address=ca, type_id=type_id)


def test_single_command_requires_bool() -> None:
    with pytest.raises(TypeError, match="boolean"):
        build_command(_cmd_point("C_SC_NA_1"), 1)
    command = build_command(_cmd_point("C_SC_NA_1"), False)
    assert isinstance(command, iec101.SingleCommand)
    assert command.value is iec101.SingleValue.OFF
    assert command.select is False


def test_double_command_requires_1_or_2() -> None:
    with pytest.raises(ValueError, match="1 .OFF. or 2 .ON."):
        build_command(_cmd_point("C_DC_NA_1"), 3)
    command = build_command(_cmd_point("C_DC_NA_1"), 2)
    assert isinstance(command, iec101.DoubleCommand)
    assert command.value is iec101.DoubleValue.ON


def test_normalized_setpoint_range() -> None:
    with pytest.raises(ValueError, match=r"-1\.\.1"):
        build_command(_cmd_point("C_SE_NA_1"), 1.5)
    command = build_command(_cmd_point("C_SE_NA_1"), -0.25)
    assert isinstance(command, iec101.NormalizedCommand)


def test_scaled_setpoint_int16_range() -> None:
    with pytest.raises(ValueError, match="int16"):
        build_command(_cmd_point("C_SE_NB_1"), 32768)
    command = build_command(_cmd_point("C_SE_NB_1"), 32767)
    assert isinstance(command, iec101.ScaledCommand)


def test_short_floating_setpoint_requires_finite() -> None:
    with pytest.raises(ValueError, match="finite"):
        build_command(_cmd_point("C_SE_NC_1"), float("inf"))
    command = build_command(_cmd_point("C_SE_NC_1"), 42.0)
    assert isinstance(command, iec101.FloatingCommand)


def test_build_command_rejects_bool_as_numeric_and_unknown_type() -> None:
    with pytest.raises(TypeError, match="numeric"):
        build_command(_cmd_point("C_SE_NC_1"), True)
    with pytest.raises(Exception, match="unsupported command type"):
        build_command(_cmd_point("C_RC_NA_1"), 1)
