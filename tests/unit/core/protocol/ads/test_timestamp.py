"""ADS 通知时标归一化：FILETIME 是 UTC 绝对时刻，与时区/DST 无关。

针对 pyads 3.6.0（poetry.lock 锁定）验证：

- aware datetime（任意时区偏移、含夏令时）转换后保持同一瞬间；
- naive datetime 只能按 UTC-naive 挂 UTC，不得按主机本地时区解释；
- 非 datetime 输入回退为本地接收时刻（source="local"）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from core.infrastructure.protocol.ads.subscription import _normalize_timestamp


def test_aware_utc_passthrough() -> None:
    ts = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    result, source = _normalize_timestamp(ts)
    assert result == ts
    assert result.utcoffset() == timedelta(0)
    assert source == "device"


def test_aware_non_utc_offset_converts_same_instant() -> None:
    # UTC+05:30（不实行夏令时的非整点偏移）
    ts = datetime(2026, 3, 1, 17, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    result, source = _normalize_timestamp(ts)
    assert result == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert source == "device"


def test_aware_est_and_edt_convert_same_instant_acpect_dst_boundary() -> None:
    # 同一本地时钟表示在 EST（UTC-5）与 EDT（UTC-4）下对应不同瞬间；
    # 归一化必须尊重绝对时刻，而不是做固定时区减法。
    est = datetime(2026, 1, 15, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    edt = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    result_est, _ = _normalize_timestamp(est)
    result_edt, _ = _normalize_timestamp(edt)
    assert result_est == datetime(2026, 1, 15, 17, 0, tzinfo=UTC)
    assert result_edt == datetime(2026, 7, 15, 16, 0, tzinfo=UTC)


def test_dst_spring_forward_gap_keeps_absolute_instant() -> None:
    # 纽约 2026-03-08 02:00→03:00 跳变；取跳变后的 EDT 时刻。
    ts = datetime(2026, 3, 8, 3, 30, tzinfo=ZoneInfo("America/New_York"))
    result, source = _normalize_timestamp(ts)
    assert result == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert source == "device"


def test_naive_datetime_is_treated_as_utc_naive_not_host_local() -> None:
    # FILETIME 按定义是 UTC：naive 值直接挂 UTC，不做任何时移。
    ts = datetime(2026, 3, 1, 12, 0)
    result, source = _normalize_timestamp(ts)
    assert result == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert source == "device"


def test_non_datetime_falls_back_to_local_utc() -> None:
    before = datetime.now(UTC)
    result, source = _normalize_timestamp(None)
    after = datetime.now(UTC)
    assert source == "local"
    assert result.tzinfo is not None
    assert before <= result <= after
