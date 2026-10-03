"""soak 时长覆盖参数解析的回归测试。

背景：ci-qualification.yml 将 ``duration_override_s``（默认空串）写入
``WIND_HUB_SOAK_DURATION_S``；旧实现 ``float(os.environ.get(..., default))``
在变量存在但为空时对 ``""`` 调 ``float`` 抛 ``ValueError``，导致不填
override 的默认路径（smoke_1h / prerelease_8h / rc_24h）全部失败。
"""

from __future__ import annotations

import pytest

from tests.reliability.soak.test_long_running import _DURATION_S, resolve_duration_s


@pytest.mark.parametrize("profile", sorted(_DURATION_S))
def test_unset_falls_back_to_profile_default(profile: str) -> None:
    assert resolve_duration_s(profile, None) == _DURATION_S[profile]


@pytest.mark.parametrize("profile", sorted(_DURATION_S))
def test_empty_string_falls_back_to_profile_default(profile: str) -> None:
    assert resolve_duration_s(profile, "") == _DURATION_S[profile]


@pytest.mark.parametrize("profile", sorted(_DURATION_S))
def test_whitespace_only_falls_back_to_profile_default(profile: str) -> None:
    assert resolve_duration_s(profile, "   \t\n ") == _DURATION_S[profile]


@pytest.mark.parametrize(
    ("override", "expected"),
    [("60", 60.0), ("60.5", 60.5), (" 120 ", 120.0)],
)
def test_valid_number_overrides_default(override: str, expected: float) -> None:
    assert resolve_duration_s("smoke_1h", override) == expected


@pytest.mark.parametrize("override", ["abc", "1h", "60s", "1,5"])
def test_invalid_number_fails_loudly(override: str) -> None:
    with pytest.raises(ValueError, match="WIND_HUB_SOAK_DURATION_S"):
        resolve_duration_s("smoke_1h", override)


@pytest.mark.parametrize("override", ["0", "-10"])
def test_non_positive_fails_loudly(override: str) -> None:
    with pytest.raises(ValueError, match="必须为正数"):
        resolve_duration_s("smoke_1h", override)


@pytest.mark.parametrize(
    "override",
    ["nan", "NaN", "inf", "+inf", "-inf", "Infinity"],
)
def test_non_finite_fails_loudly(override: str) -> None:
    # float() 接受 nan/inf 字面量，且 nan <= 0 为 False——必须显式拒非有限值。
    with pytest.raises(ValueError, match="有限正数"):
        resolve_duration_s("smoke_1h", override)
