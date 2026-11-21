"""CLI-only publication of a complete configuration directory into a real service."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from contracts import configuration_repository_pb2 as pb
from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _full_root(live: LiveConfiguration) -> Path:
    directory = live.root / "configs"
    (directory / "templates/point_tables").mkdir(parents=True)
    for filename in ("units_standard_v1.2.0.yaml", "business_points_standard_v1.4.0.yaml"):
        shutil.copyfile(
            PROJECT_ROOT / "configs/templates" / filename,
            directory / "templates" / filename,
        )
    (directory / "templates/point_tables/point_table_wtg_v1.0.0.yaml").write_text(
        "schema_version: 1\nfile_version: 1.0.0\ntemplate_id: wtg\n"
        "device_type: turbine\nprotocol: modbus\npoints: {}\n",
        encoding="utf-8",
    )
    return directory


@pytest.mark.asyncio
async def test_full_root_publish_noop_removal_and_historical_query(
    live: LiveConfiguration,
) -> None:
    source = _full_root(live)
    validated = live.cli("config", "publish", "--root", str(source), "--validate-only")
    assert validated["data"]["valid"] is True
    assert validated["data"]["published"] is False
    first = live.cli("config", "publish", "--root", str(source))
    first_id = int(first["data"]["revision_id"])
    assert first_id > 0
    assert first["data"]["file_count"] == 3

    repeated = live.cli("config", "publish", "--root", str(source))
    assert int(repeated["data"]["revision_id"]) == first_id
    assert repeated["data"]["already_applied"] is True

    (source / "templates/point_tables/point_table_wtg_v1.0.0.yaml").unlink()
    removed = live.cli("config", "publish", "--root", str(source))
    assert int(removed["data"]["revision_id"]) == first_id + 1

    repository, _common, channel = live.stubs("viewer")
    try:
        current = await repository.GetCurrentRevision(pb.GetCurrentRevisionRequest())
        assert current.revision_id == first_id + 1
        assert len(current.manifest_sha256) == 64

        historical = await repository.GetPointTableBundle(
            pb.GetPointTableBundleRequest(point_table_id="point_table_wtg_v1.0.0")
        )
        assert historical.device_type == "turbine"
        assert {d.config_id.config_type for d in historical.documents} == {
            "units",
            "business_points",
            "point_table",
        }
        assert len(historical.manifest_sha256) == 64
    finally:
        await channel.close()
