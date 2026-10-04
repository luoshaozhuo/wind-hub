"""Unit tests for the rotation-policy abstraction (adapter/outbound/sink/file/rotation.py)."""

from __future__ import annotations

from datetime import UTC, datetime

from wind_hub_collector.adapter.outbound.sink.file.rotation import (
    CompositeRotation,
    NoRotation,
    RotationPolicy,
    SizeRotation,
    TimeRotation,
    build_rotation,
)

_BYTES_PER_MB = 1024 * 1024
_SECONDS_PER_HOUR = 3600.0
_TS = datetime(2026, 9, 16, 14, 30, 25, tzinfo=UTC)
_TS_MICROS = datetime(2026, 9, 16, 14, 30, 25, 1, tzinfo=UTC)


class TestSizeRotation:
    def test_below_threshold_does_not_rotate(self) -> None:
        policy = SizeRotation(max_size_mb=1.0)
        assert policy.should_rotate(_BYTES_PER_MB - 1, age_seconds=0.0) is False

    def test_at_threshold_rotates(self) -> None:
        policy = SizeRotation(max_size_mb=1.0)
        assert policy.should_rotate(_BYTES_PER_MB, age_seconds=0.0) is True

    def test_above_threshold_rotates(self) -> None:
        policy = SizeRotation(max_size_mb=2.0)
        assert policy.should_rotate(3 * _BYTES_PER_MB, age_seconds=0.0) is True

    def test_suffix_format(self) -> None:
        assert SizeRotation(max_size_mb=1.0).rotation_suffix(_TS) == "20260916_143025_000000"

    def test_suffix_includes_microseconds(self) -> None:
        policy = SizeRotation(max_size_mb=1.0)
        # 同一秒内两次滚动（仅微秒不同）必须生成不同的归档名，避免秒级碰撞
        assert policy.rotation_suffix(_TS) != policy.rotation_suffix(_TS_MICROS)


class TestTimeRotation:
    def test_below_age_does_not_rotate(self) -> None:
        policy = TimeRotation(max_age_hours=1.0)
        assert policy.should_rotate(0, age_seconds=_SECONDS_PER_HOUR - 1) is False

    def test_at_age_rotates(self) -> None:
        policy = TimeRotation(max_age_hours=1.0)
        assert policy.should_rotate(0, age_seconds=_SECONDS_PER_HOUR) is True

    def test_suffix_format(self) -> None:
        assert TimeRotation(max_age_hours=1.0).rotation_suffix(_TS) == "20260916_143025_000000"


class TestCompositeRotation:
    def test_any_policy_triggers(self) -> None:
        composite: RotationPolicy = CompositeRotation(
            [SizeRotation(max_size_mb=1.0), TimeRotation(max_age_hours=24.0)]
        )
        # 大小超限、时长未超限 → 任一触发即滚动
        assert composite.should_rotate(_BYTES_PER_MB, age_seconds=0.0) is True
        # 大小未超限、时长超限 → 滚动
        assert composite.should_rotate(0, age_seconds=24 * _SECONDS_PER_HOUR) is True

    def test_none_trigger_means_no_rotation(self) -> None:
        composite: RotationPolicy = CompositeRotation(
            [SizeRotation(max_size_mb=1.0), TimeRotation(max_age_hours=24.0)]
        )
        assert composite.should_rotate(1, age_seconds=1.0) is False

    def test_suffix_delegates_to_first_policy(self) -> None:
        composite = CompositeRotation([SizeRotation(max_size_mb=1.0)])
        assert composite.rotation_suffix(_TS) == "20260916_143025_000000"


class TestNoRotation:
    def test_never_rotates(self) -> None:
        policy = NoRotation()
        assert policy.should_rotate(10**9, age_seconds=10**9) is False

    def test_suffix_format(self) -> None:
        assert NoRotation().rotation_suffix(_TS) == "20260916_143025_000000"


class TestBuildRotation:
    def test_no_limits_yields_no_rotation(self) -> None:
        policy = build_rotation(None, None)
        assert isinstance(policy, NoRotation)

    def test_size_only(self) -> None:
        policy = build_rotation(5.0, None)
        assert isinstance(policy, SizeRotation)

    def test_time_only(self) -> None:
        policy = build_rotation(None, 2.0)
        assert isinstance(policy, TimeRotation)

    def test_both_yields_composite(self) -> None:
        policy = build_rotation(5.0, 2.0)
        assert isinstance(policy, CompositeRotation)
