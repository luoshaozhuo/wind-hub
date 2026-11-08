"""One-time JetStream provisioning, independent from Collector and Sink Service."""

from __future__ import annotations

import argparse
import asyncio

import nats
from nats.js.api import DiscardPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError


async def ensure_history_stream(
    servers: tuple[str, ...],
    *,
    stream: str = "WH_HISTORY",
    max_age_days: int = 7,
) -> None:
    """Provision the history-only Stream without touching realtime Subjects."""
    if max_age_days <= 0:
        raise ValueError("max_age_days must be positive")
    client = await nats.connect(servers=list(servers))
    try:
        jetstream = client.jetstream()
        try:
            current = await jetstream.stream_info(stream)
        except NotFoundError:
            await jetstream.add_stream(
                config=StreamConfig(
                    name=stream,
                    subjects=["wh.hist.batch.>"],
                    discard=DiscardPolicy.NEW,
                    storage=StorageType.FILE,
                    max_age=max_age_days * 86_400,
                )
            )
        else:
            if list(current.config.subjects or []) != ["wh.hist.batch.>"]:
                raise ValueError("history Stream subjects differ from wh.hist.batch.>")
    finally:
        await client.close()


async def ensure_dlq_stream(
    servers: tuple[str, ...],
    *,
    stream: str = "WH_DLQ",
    max_age_days: int = 30,
) -> None:
    """Provision a distinct persistent DLQ before starting reliable Sinks."""
    if max_age_days <= 0:
        raise ValueError("max_age_days must be positive")
    client = await nats.connect(servers=list(servers))
    try:
        jetstream = client.jetstream()
        try:
            current = await jetstream.stream_info(stream)
        except NotFoundError:
            await jetstream.add_stream(
                config=StreamConfig(
                    name=stream,
                    subjects=["wh.dlq.batch.>"],
                    storage=StorageType.FILE,
                    discard=DiscardPolicy.NEW,
                    max_age=max_age_days * 86_400,
                )
            )
        else:
            if list(current.config.subjects or []) != ["wh.dlq.batch.>"]:
                raise ValueError("DLQ Stream subjects differ from wh.dlq.batch.>")
    finally:
        await client.close()


def main() -> None:
    """Provision NATS JetStream once during deployment."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats", action="append", required=True)
    parser.add_argument("--stream", default="WH_HISTORY")
    parser.add_argument("--max-age-days", type=int, default=7)
    parser.add_argument("--dlq-stream", default="WH_DLQ")
    parser.add_argument("--dlq-max-age-days", type=int, default=30)
    args = parser.parse_args()

    async def provision() -> None:
        await ensure_history_stream(
            tuple(args.nats), stream=args.stream, max_age_days=args.max_age_days
        )
        await ensure_dlq_stream(
            tuple(args.nats),
            stream=args.dlq_stream,
            max_age_days=args.dlq_max_age_days,
        )

    asyncio.run(provision())


if __name__ == "__main__":
    main()
