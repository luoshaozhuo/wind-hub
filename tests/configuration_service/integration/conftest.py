"""CLI-launched real Configuration Service fixture."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from configuration_service.infrastructure.adapters.outbound.sqlite.repository import (
    SQLiteRepository,
)
from tests.support.configuration_live import (
    LiveConfiguration,
    reserve_port,
    write_live_configuration,
)


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveConfiguration]:
    root = tmp_path_factory.mktemp("configuration-live")
    live_service = write_live_configuration(root, reserve_port())
    started = live_service.cli("service", "start", "configuration", "--yes", check=False)
    if started["exit_code"] != 0:
        log_path = root / "configuration.log"
        raise AssertionError(f"CLI service start failed: {started}; log={log_path.read_text()}")
    try:
        yield live_service
    finally:
        stopped = live_service.cli("service", "stop", "configuration", "--yes", check=False)
        assert stopped["exit_code"] == 0, stopped
        # A new owner can initialize only after flock and every DB handle are released.
        repository = SQLiteRepository(live_service.database_path, application_version="test")
        repository.initialize()
        repository.close()
        assert list((live_service.root / "backups").glob("*.part")) == []
        log_text = (live_service.root / "configuration.log").read_text(encoding="utf-8")
        assert "BEGIN PRIVATE KEY" not in log_text
        assert "configuration-integration-secret-32-bytes" not in log_text
