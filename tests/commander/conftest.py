"""Commander 测试共享夹具：真实 SQLite 仓储与 CommandControl。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from commander.application.control import CommandControl
from commander.infrastructure.event_repository import EventRepository


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[EventRepository]:
    repo = EventRepository(tmp_path / "commander.db")
    try:
        yield repo
    finally:
        repo.close()


@pytest.fixture
def control(repository: EventRepository) -> CommandControl:
    return CommandControl(repository)
