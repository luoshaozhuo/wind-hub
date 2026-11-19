"""Real NATS Core broadcast of committed Configuration revisions."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import nats
import pytest
import yaml

from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

SUBJECT = "wh.config.changed"


def _root(live: LiveConfiguration) -> Path:
    directory = live.root / "nats-root"
    tasks = directory / "templates/tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "task_nats_v1.0.0.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "template_id": "nats",
                "point_group": "nats",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return directory


@pytest.mark.asyncio
async def test_committed_revision_is_broadcast_over_real_nats(
    live: LiveConfiguration,
) -> None:
    try:
        client = await nats.connect(
            "nats://127.0.0.1:4222", connect_timeout=2, max_reconnect_attempts=0
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"real NATS not reachable for revision broadcast: {exc}")

    received: asyncio.Queue[dict[str, int]] = asyncio.Queue()

    async def listener(message: object) -> None:
        received.put_nowait(json.loads(message.data))

    sub = await client.subscribe(SUBJECT, cb=listener)
    await asyncio.sleep(0.3)
    try:
        before = int(live.cli("config", "revision")["data"].get("revision_id", 0))
        published = live.cli("config", "publish", "--root", str(_root(live)))
        assert int(published["data"]["revision_id"]) > before
        expected = int(published["data"]["revision_id"])
        payload = await asyncio.wait_for(received.get(), timeout=8)
        assert payload == {"revision_id": expected}

        # A dry run must not produce any committed revision notification.
        drained = received.qsize()
        live.cli("config", "publish", "--root", str(_root(live)), "--validate-only")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(received.get(), timeout=1.0)
        assert received.qsize() == drained
    finally:
        await sub.unsubscribe()
        await client.close()
