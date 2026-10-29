"""原生订阅/监听与读取性能检测。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from time import perf_counter
from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from core.application.protocol_contract import ProtocolCapability, ProtocolSample

from .protocol_diagnostic import create_driver, prepare_ads, release_ads, sample_dict


async def listen(
    snapshot: ConfigSnapshot,
    device_id: str,
    point_ids: Sequence[str],
    *,
    duration: float | None = None,
    timeout: float = 5.0,
    on_sample: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """限时原生监听；不以轮询模拟任何协议推送能力。"""
    if (duration is not None and duration <= 0) or timeout <= 0:
        raise ValueError("duration and timeout must be positive")
    driver = create_driver(snapshot, device_id)
    samples: list[dict[str, Any]] = []

    async def on_samples(batch: tuple[ProtocolSample, ...]) -> None:
        for sample in batch:
            record = sample_dict(sample)
            if on_sample is not None:
                on_sample(record)
            elif len(samples) < 10000:
                samples.append(record)

    handle: Any = None
    ads_prepared = False
    try:
        ads_prepared = await prepare_ads(snapshot, driver)
        async with asyncio.timeout(timeout):
            await driver.connect()
        capabilities = driver.capabilities()
        if ProtocolCapability.SUBSCRIBE in capabilities:
            handle = driver.subscribe(point_ids, on_samples)
            async with asyncio.timeout(timeout):
                await handle.start()
        elif ProtocolCapability.LISTEN in capabilities:
            async with asyncio.timeout(timeout):
                handle = await driver.listen(point_ids, on_samples)
        else:
            raise ValueError("protocol does not support native subscribe or listen")
        if duration is None:
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(duration)
        return samples
    finally:
        try:
            if handle is not None:
                async with asyncio.timeout(timeout):
                    if ProtocolCapability.SUBSCRIBE in driver.capabilities():
                        await handle.stop()
                    else:
                        await handle.close()
        finally:
            try:
                async with asyncio.timeout(timeout):
                    await driver.close()
            finally:
                await release_ads(ads_prepared)


async def benchmark(
    snapshot: ConfigSnapshot,
    device_id: str,
    point_ids: Sequence[str],
    *,
    samples: int = 10,
    interval: float = 0.2,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """按实际能力执行原生读；不将多次单点读取冒充原生批量读。"""
    if not 1 <= samples <= 200 or not 1 <= len(point_ids) <= 200:
        raise ValueError("samples and point count must be 1..200")
    if interval < 0 or timeout <= 0:
        raise ValueError("interval must be nonnegative and timeout positive")
    driver = create_driver(snapshot, device_id)
    timings: list[float] = []
    ads_prepared = False
    try:
        ads_prepared = await prepare_ads(snapshot, driver)
        async with asyncio.timeout(timeout):
            await driver.connect()
        caps = driver.capabilities()
        capability = (
            ProtocolCapability.READ if len(point_ids) == 1 else ProtocolCapability.READ_MANY
        )
        if capability not in caps:
            raise ValueError(f"protocol does not support {capability.value}")
        for index in range(samples):
            started = perf_counter()
            async with asyncio.timeout(timeout):
                if len(point_ids) == 1:
                    await driver.read_one(point_ids[0])
                else:
                    await driver.read_many(point_ids)
            timings.append((perf_counter() - started) * 1000)
            if index + 1 < samples:
                await asyncio.sleep(interval)
        ordered = sorted(timings)
        return {
            "device_id": device_id,
            "points": list(point_ids),
            "samples": samples,
            "min_ms": min(ordered),
            "mean_ms": sum(ordered) / len(ordered),
            "max_ms": max(ordered),
            "p95_ms": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
        }
    finally:
        try:
            async with asyncio.timeout(timeout):
                await driver.close()
        finally:
            await release_ads(ads_prepared)
