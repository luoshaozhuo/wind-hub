"""One-time JetStream provisioning, independent from Collector and Sink Service."""

from __future__ import annotations

import argparse
import asyncio

import nats
from nats.js.api import StorageType, StreamConfig
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
                    subjects=["wh.hist.>"],
                    storage=StorageType.FILE,
                    max_age=max_age_days * 86_400,
                )
            )
        else:
            if list(current.config.subjects or []) != ["wh.hist.>"]:
                raise ValueError("history Stream subjects differ from wh.hist.>")
    finally:
        await client.close()


def main() -> None:
    """Provision NATS JetStream once during deployment."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats", action="append", required=True)
    parser.add_argument("--stream", default="WH_HISTORY")
    parser.add_argument("--max-age-days", type=int, default=7)
    args = parser.parse_args()
    asyncio.run(
        ensure_history_stream(
            tuple(args.nats),
            stream=args.stream,
            max_age_days=args.max_age_days,
        )
    )


if __name__ == "__main__":
    main()
