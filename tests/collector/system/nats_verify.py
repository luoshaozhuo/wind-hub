"""Real NATS verification clients for Collector system tests.

These are independent observers: they connect to the same real broker as the
Collector and inspect what the broker actually accepted (Core NATS messages or
JetStream PubAck-confirmed messages). They never run inside the Collector.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import nats
from nats.aio.msg import Msg
from nats.js.errors import APIError

REALTIME_SUBJECT = "wh.rt.>"
RELIABLE_SUBJECT = "wh.hist.>"
HIST_STREAM = "WH_HIST"


async def collect_realtime(url: str, *, count: int, timeout: float) -> list[dict[str, Any]]:
    """Collect at least ``count`` Core NATS messages through an independent client."""
    client = await nats.connect(url)
    received: list[dict[str, Any]] = []
    queue: asyncio.Queue[Msg] = asyncio.Queue()

    async def on_message(message: Msg) -> None:
        received.append(json.loads(message.data))
        await queue.put(message)

    subscription = await client.subscribe(REALTIME_SUBJECT, cb=on_message)
    try:
        for _ in range(count):
            await asyncio.wait_for(queue.get(), timeout=timeout)
        await asyncio.sleep(0.3)  # let the rest of the same poll batch arrive
    finally:
        await subscription.unsubscribe()
        await client.close()
    return received


async def ensure_hist_stream(url: str) -> None:
    """Create the JetStream stream that downstream Sink infrastructure owns."""
    client = await nats.connect(url)
    try:
        jetstream = client.jetstream()
        # Idempotent across tests sharing one broker.
        with contextlib.suppress(APIError):
            await jetstream.add_stream(name=HIST_STREAM, subjects=["wh.hist.>"])
    finally:
        await client.close()


async def collect_reliable(url: str, *, count: int, timeout: float) -> list[dict[str, Any]]:
    """Collect ``count`` PubAck-confirmed JetStream messages via an ephemeral consumer."""
    client = await nats.connect(url)
    received: list[dict[str, Any]] = []
    queue: asyncio.Queue[Msg] = asyncio.Queue()

    async def on_message(message: Msg) -> None:
        received.append(json.loads(message.data))
        await message.ack()
        await queue.put(message)

    jetstream = client.jetstream()
    subscription = await jetstream.subscribe(
        RELIABLE_SUBJECT,
        cb=on_message,
        manual_ack=True,
    )
    try:
        for _ in range(count):
            await asyncio.wait_for(queue.get(), timeout=timeout)
    finally:
        await subscription.unsubscribe()
        await client.close()
    return received
