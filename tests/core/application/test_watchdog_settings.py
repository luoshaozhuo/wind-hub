"""Watchdog runtime settings contract."""

from __future__ import annotations

from typing import Any

import pytest

from core.application.settings import RuntimeSettings


def test_watchdog_timing_defaults() -> None:
    settings = RuntimeSettings()
    assert settings.watchdog_interval == 5.0
    assert settings.health_timeout == 1.0


def test_watchdog_timing_accepts_positive_values() -> None:
    settings = RuntimeSettings(watchdog_interval=2, health_timeout=0.25)
    assert settings.watchdog_interval == 2.0
    assert settings.health_timeout == 0.25


@pytest.mark.parametrize("field", ["watchdog_interval", "health_timeout"])
@pytest.mark.parametrize("bad", [0, -0.1, float("nan"), float("inf")])
def test_watchdog_timing_rejects_non_positive_or_non_finite(
    field: str,
    bad: Any,
) -> None:
    with pytest.raises(ValueError):
        RuntimeSettings(**{field: bad})
