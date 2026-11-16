"""Redis Compose fixture uses private names and dynamic ports; no Docker required."""

from __future__ import annotations

import subprocess

import pytest

from tests.fixtures.services import compose

pytestmark = pytest.mark.unit


def test_redis_compose_retries_port_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    ports = iter((45101, 45102))

    def fake_compose(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0] == "up" and sum(call[0] == "up" for call in calls) == 1:
            return subprocess.CompletedProcess(args, 1, "", "port is already allocated")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(compose, "_free_port", lambda: next(ports))
    monkeypatch.setattr(compose, "_compose", fake_compose)
    monkeypatch.setattr(compose, "_HOST_PORT", None)
    monkeypatch.setattr(compose, "_PROJECT_NAME", None)
    try:
        assert compose.compose_up() == 45102
        assert compose._HOST_PORT == 45102
        assert compose._PROJECT_NAME is not None
        assert compose._PROJECT_NAME.startswith("wind-hub-test-")
        assert [call[0] for call in calls] == ["up", "down", "up"]
    finally:
        compose.compose_down()
    assert calls[-1][0] == "down"
    assert compose._HOST_PORT is None
    assert compose._PROJECT_NAME is None


def test_redis_compose_does_not_retry_non_port_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_compose(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0] == "up":
            return subprocess.CompletedProcess(args, 1, "", "image manifest not found")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(compose, "_free_port", lambda: 45103)
    monkeypatch.setattr(compose, "_compose", fake_compose)
    monkeypatch.setattr(compose, "_HOST_PORT", None)
    monkeypatch.setattr(compose, "_PROJECT_NAME", None)
    with pytest.raises(compose.ComposeError, match="manifest not found"):
        compose.compose_up()
    assert [call[0] for call in calls] == ["up", "down"]
    assert compose._PROJECT_NAME is None
