"""JetStream audit publisher with durable event identity."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol


class JetStreamClient(Protocol):
    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: Mapping[str, Any] | None = None,
    ) -> object: ...


class AuditPublisher:
    """Publish immutable journal events; acknowledgement is required."""

    def __init__(self, client: JetStreamClient, subject: str, stream: str) -> None:
        if not subject.strip():
            raise ValueError("audit subject must not be empty")
        self._client = client
        if not stream.strip():
            raise ValueError("audit stream must not be empty")
        self._subject = subject
        self._stream = stream

    async def publish(self, event: Mapping[str, Any]) -> None:
        event_id = event.get("event_id")
        event_type = event.get("event_type")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("audit event_id required")
        if event_type not in (
            "ACCEPTED",
            "CONNECTING",
            "EXECUTING",
            "COMPLETED",
            "RECOVERY_FENCE_COMMITTED",
        ):
            raise ValueError("invalid Commander command event type")
        # Explicit schema excludes database-only delivery flags and timestamps.
        body = {
            key: value
            for key, value in event.items()
            if key not in {"publish_state", "published_at", "attempts"}
        }
        payload = json.dumps(body, sort_keys=True, ensure_ascii=False, default=_json_value).encode(
            "utf-8"
        )
        ack = await self._client.publish(
            self._subject,
            payload,
            headers={"Nats-Msg-Id": event_id},
        )
        if getattr(ack, "stream", None) != self._stream:
            raise RuntimeError("audit PubAck received from unexpected stream")


def _json_value(value: object) -> Any:
    if isinstance(value, bytes):
        return json.loads(value.decode("utf-8"))
    raise TypeError(f"unsupported audit value type: {type(value).__name__}")
