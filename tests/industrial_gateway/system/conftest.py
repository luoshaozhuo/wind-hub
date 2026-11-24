"""Gateway system tests: provide the Bearer secrets required at process start."""

from __future__ import annotations

import pytest

_TOKEN_ENVS = (
    "WIND_HUB_TEST_GATEWAY_GW_ADMIN_TOKEN",
    "WIND_HUB_TEST_GATEWAY_COMMANDER_TARGET_TOKEN",
)


@pytest.fixture(autouse=True)
def _gateway_process_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _TOKEN_ENVS:
        monkeypatch.setenv(name, "t" * 40)
