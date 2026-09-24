"""Unit tests for process-level ADS router init and one-shot route repair."""

from __future__ import annotations

import asyncio

import pytest

from wind_hub.adapter.outbound.protocol.ads import router
from wind_hub.config.schema import ADSSystemConfig


def _ads_cfg(*, enabled: bool = True) -> ADSSystemConfig:
    return ADSSystemConfig(
        local_ams_net_id="192.168.151.244.1.2",
        local_ip="192.168.151.244",
        route_repair={
            "enabled": enabled,
            "route_name": "PFR",
            "username": "Administrator",
            "password": "",
        },
    )


@pytest.fixture(autouse=True)
def _reset_router(monkeypatch: pytest.MonkeyPatch) -> object:
    router.reset_for_tests()
    # 本机初始化相关的 pyads 调用统一替换为 no-op——单元测试不触碰真实 ADS 端口。
    monkeypatch.setattr("pyads.open_port", lambda: None)
    monkeypatch.setattr("pyads.set_local_address", lambda net_id: None)
    yield
    router.reset_for_tests()


class TestEnsureLocalInitialized:
    async def test_initializes_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """open_port / set_local_address 每进程只执行一次（含并发调用）。"""
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


class TestRepairRouteOnce:
    async def test_not_initialized_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "pyads.add_route_to_plc",
            lambda *a, **k: pytest.fail("add_route_to_plc must not be called"),
        )
        assert await router.repair_route_once("192.168.151.40") is False

    async def test_disabled_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "pyads.add_route_to_plc",
            lambda *a, **k: pytest.fail("add_route_to_plc must not be called"),
        )
        await router.ensure_local_initialized(_ads_cfg(enabled=False))
        assert await router.repair_route_once("192.168.151.40") is False

    async def test_repairs_once_with_expected_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[tuple[tuple[object, ...], dict[str, object]]] = []
        monkeypatch.setattr(
            "pyads.add_route_to_plc",
            lambda *a, **k: recorded.append((a, k)),
        )
        await router.ensure_local_initialized(_ads_cfg())

        assert await router.repair_route_once("192.168.151.40") is True
        # 同一 PLC 第二次调用不再执行 add_route
        assert await router.repair_route_once("192.168.151.40") is False

        assert len(recorded) == 1
        args, kwargs = recorded[0]
        assert args == ("192.168.151.244.1.2", "192.168.151.244", "192.168.151.40")
        assert kwargs == {"username": "Administrator", "password": "", "route_name": "PFR"}

    async def test_repair_failure_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = 0

        def _failing(*a: object, **k: object) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("plc unreachable")

        monkeypatch.setattr("pyads.add_route_to_plc", _failing)
        await router.ensure_local_initialized(_ads_cfg())

        assert await router.repair_route_once("192.168.151.40") is False
        assert await router.repair_route_once("192.168.151.40") is False
        assert calls == 1  # 失败也不反复 add route
