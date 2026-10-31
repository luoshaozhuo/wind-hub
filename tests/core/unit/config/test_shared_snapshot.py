"""Typed shared configuration snapshot validation."""

from __future__ import annotations

from pathlib import Path

from core.domain import DeviceId
from core.infrastructure.config.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
)
from core.infrastructure.config.shared_snapshot import build_shared_snapshot

_CONFIGS = Path(__file__).resolve().parents[4] / "configs"


def test_shared_snapshot_resolves_typed_devices_and_points() -> None:
    documents = SharedDocumentLoader(
        SharedConfigPaths(
            instances=_CONFIGS / "instances",
            templates=_CONFIGS / "templates",
        )
    ).load()

    snapshot = build_shared_snapshot(documents)

    assert DeviceId("WT01") in snapshot.site.devices
    device = snapshot.site.devices[DeviceId("WT01")]
    model = snapshot.device_models[device.device_model_id]
    table = snapshot.point_tables[model.point_table_id]
    assert table.protocol.name == "modbus"
    assert "active_power" in table.points
    assert snapshot.revision == documents.revision
