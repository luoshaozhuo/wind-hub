"""Typed shared configuration snapshot validation."""

from __future__ import annotations

from pathlib import Path

from configuration.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
)
from configuration.shared_snapshot import build_shared_snapshot
from core.domain import DeviceId

_CONFIGS = Path(__file__).resolve().parents[4] / "configs"


def test_shared_snapshot_resolves_typed_devices_and_points() -> None:
    documents = SharedDocumentLoader(
        SharedConfigPaths(
            instances=_CONFIGS / "instances",
            templates=_CONFIGS / "templates",
            site_file=_CONFIGS / "instances" / "site.yaml",
            devices_file=_CONFIGS / "instances" / "devices.yaml",
        )
    ).load()

    snapshot = build_shared_snapshot(documents)

    assert DeviceId("GW01") in snapshot.site.devices
    assert DeviceId("TZ01") in snapshot.site.devices

    ads_device = snapshot.site.devices[DeviceId("GW01")]
    ads_model = snapshot.device_models[ads_device.device_model_id]
    ads_table = snapshot.point_tables[ads_model.point_table_id]
    assert ads_table.protocol.name == "ads"
    assert "a02_dm_pitchangle1" in ads_table.points

    device = snapshot.site.devices[DeviceId("TZ01")]
    model = snapshot.device_models[device.device_model_id]
    table = snapshot.point_tables[model.point_table_id]
    assert table.protocol.name == "modbus"
    assert "measured_power" in table.points
    assert snapshot.revision == documents.revision
