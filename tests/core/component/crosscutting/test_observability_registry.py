"""观测注册表并发和关闭资源所有权回归。"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.infrastructure.crosscutting.observability import (
    LoggingSettings,
    ObservabilitySettings,
    configure_observability,
)

pytestmark = [pytest.mark.component, pytest.mark.reliability, pytest.mark.concurrency]


def _settings() -> ObservabilitySettings:
    return ObservabilitySettings(logging=LoggingSettings(console=False))


async def test_concurrent_assembly_reuses_handle_and_releases_leases() -> None:
    settings = _settings()
    with ThreadPoolExecutor(max_workers=16) as pool:
        handles = list(
            pool.map(
                lambda _: configure_observability(settings, service_name="registry-race"),
                range(64),
            )
        )
    handle = handles[0]
    assert all(item is handle for item in handles)
    assert handle._leases == 64

    for _ in range(63):
        await handle.close()
        assert not handle._closed

    await handle.close()
    assert handle._closed
    replacement = configure_observability(settings, service_name="registry-race")
    assert replacement is not handle
    await replacement.close()


async def test_shutdown_timeout_does_not_duplicate_provider_shutdown() -> None:
    settings = _settings()
    handle = configure_observability(settings, service_name="registry-shutdown")
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    class BlockingProvider:
        def shutdown(self) -> None:
            calls.append(1)
            started.set()
            assert release.wait(timeout=5)

    handle._provider = BlockingProvider()
    try:
        with pytest.raises(TimeoutError, match="tracing provider shutdown timed out"):
            await handle.close(timeout=0.1)
        assert await asyncio.to_thread(started.wait, 1)
        with pytest.raises(RuntimeError, match="shutdown has started"):
            configure_observability(settings, service_name="registry-shutdown")
    finally:
        release.set()

    await handle.close(timeout=2)
    assert calls == [1]
    assert handle._closed
    replacement = configure_observability(settings, service_name="registry-shutdown")
    assert replacement is not handle
    await replacement.close()


async def test_conflicting_configuration_does_not_acquire_lease() -> None:
    settings = _settings()
    handle = configure_observability(settings, service_name="registry-config")
    other = ObservabilitySettings(logging=LoggingSettings(console=False, level="DEBUG"))
    try:
        with pytest.raises(ValueError, match="different settings"):
            configure_observability(other, service_name="registry-config")
        assert handle._leases == 1
    finally:
        await handle.close()
