"""Explicit, delivery-scoped JetStream DLQ replay. Defaults to dry-run.

Never consumes or ACKs DLQ records. Successful republishing is not proof of
downstream delivery; the original source identity is preserved.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json

import nats
from nats.js.errors import NotFoundError

from contracts.sample_message import SampleMessage


async def replay(
    *,
    servers: list[str],
    dlq_stream: str,
    delivery_id: str,
    after_sequence: int,
    limit: int,
    max_rate: float,
    execute: bool,
) -> tuple[int, int]:
    """Inspect bounded DLQ sequences and publish only confirmed target replays."""
    if not delivery_id or limit < 1 or max_rate <= 0 or after_sequence < 0:
        raise ValueError("invalid replay bounds")
    client = await nats.connect(servers=servers)
    scanned = 0
    published = 0
    try:
        js = client.jetstream()
        stream = await js.stream_info(dlq_stream)
        first = max(stream.state.first_seq, after_sequence + 1)
        last = min(stream.state.last_seq, first + limit - 1)
        for sequence in range(first, last + 1):
            try:
                raw = await js.get_msg(dlq_stream, seq=sequence)
            except NotFoundError:
                continue
            scanned += 1
            record = json.loads(raw.data.decode("utf-8"))
            if record.get("delivery_id") != delivery_id:
                continue
            subject = record["subject"]
            payload = base64.b64decode(record["payload_base64"], validate=True)
            sample = SampleMessage.decode(payload, "reliable")
            if sample.subject("reliable") != subject:
                raise ValueError(f"DLQ record {sequence} has a mismatched source subject")
            if execute:
                stable_id = hashlib.sha256(
                    f"{dlq_stream}:{sequence}:{delivery_id}".encode("utf-8")
                ).hexdigest()
                await js.publish(
                    subject,
                    payload,
                    headers={
                        "Nats-Msg-Id": f"sink-replay-{stable_id}",
                        "Wh-Replay-Delivery": delivery_id,
                    },
                    timeout=5.0,
                )
                published += 1
                await asyncio.sleep(1.0 / max_rate)
            else:
                print(f"DRY_RUN DLQ sequence={sequence} delivery={delivery_id}")
        return scanned, published
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", action="append", required=True)
    parser.add_argument("--dlq-stream", required=True)
    parser.add_argument("--delivery-id", required=True)
    parser.add_argument("--after-sequence", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-rate", type=float, default=50.0)
    parser.add_argument(
        "--execute", action="store_true", help="Publish replays; without this only inspect"
    )
    args = parser.parse_args()
    scanned, published = asyncio.run(
        replay(
            servers=args.server,
            dlq_stream=args.dlq_stream,
            delivery_id=args.delivery_id,
            after_sequence=args.after_sequence,
            limit=args.limit,
            max_rate=args.max_rate,
            execute=args.execute,
        )
    )
    print(f"scanned={scanned} published={published}")


if __name__ == "__main__":
    main()
