"""ADS 本机 AMS identity 初始化单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from wind_hub.adapter.outbound.protocol.ads import router
from wind_hub.config.schema import ADSSystemConfig


def _ads_cfg() -> ADSSystemConfig:
    return ADSSystemConfig(
        local_ams_net_id="192.168.151.244.1.2",
        local_ip="192.168.151.244",
        username="Administrator",
        password="",
    )


@pytest.fixture(autouse=True)
def _reset_router(monkeypatch: pytest.MonkeyPatch) -> object:
    router.reset_for_tests()
    monkeypatch.setattr("pyads.open_port", lambda: None)
    monkeypatch.setattr("pyads.set_local_address", lambda net_id: None)
    yield
    router.reset_for_tests()


async def test_initializes_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {"open_port": 0, "set_local": []}
    monkeypatch.setattr(
        "pyads.open_port",
        lambda: calls.__setitem__("open_port", int(calls["open_port"]) + 1),
    )
    monkeypatch.setattr(
        "pyads.set_local_address",
        lambda net_id: calls["set_local"].append(net_id),  # type: ignore[attr-defined]
    )
    cfg = _ads_cfg()
    await asyncio.gather(
        router.ensure_local_initialized(cfg),
        router.ensure_local_initialized(cfg),
    )
    await router.ensure_local_initialized(cfg)

    assert calls["open_port"] == 1
    assert calls["set_local"] == ["192.168.151.244.1.2"]
