"""Missing physical wide tables are empty; conflicting ownership is not."""

from __future__ import annotations

from pathlib import Path

import pytest

from storage_service.application.wide_query import (
    WideCursorCodec,
    WideHistoryFilter,
    WideHistoryQueryService,
)
from storage_service.domain.errors import TDengineSchemaConflictError, WideTableNotCreatedError
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore


class _Backend:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.reads = 0

    async def verify_existing_wide_table(
        self,
        schema: WideSchema,
        *,
        wind_farm_id: str,
        device_id: str,
        device_type: str,
    ) -> None:
        raise self.failure

    async def fetch_wide_history(self, schema: WideSchema, query: WideHistoryFilter) -> tuple[()]:
        self.reads += 1
        raise AssertionError("unverified wide table must not be queried")


def _schema() -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_test_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("p", "DOUBLE"),),
        max_row_bytes=65536,
    )


def _filter(*, quality: bool) -> WideHistoryFilter:
    return WideHistoryFilter(
        wind_farm_id="farm",
        device_id="WT001",
        device_config_id="device_test_v1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        start_time_us=1,
        end_time_us=10_000_000,
        point_ids=("p",),
        limit=10,
        qualities=(1,) if quality else (),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", [False, True])
async def test_never_created_wide_table_is_empty(quality: bool) -> None:
    backend = _Backend(WideTableNotCreatedError("no registration"))
    query = WideHistoryQueryService(
        backend=backend,
        codec=WideCursorCodec(b"k" * 32),
    )
    page = await query.query(
        schema=_schema(),
        filter_=_filter(quality=quality),
        device_type="wtg",
    )
    assert page.complete
    assert page.rows == ()
    assert page.next_page_token == ""
    assert backend.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", [False, True])
async def test_conflicting_wide_ownership_remains_blocked(quality: bool) -> None:
    backend = _Backend(TDengineSchemaConflictError("identity mismatch"))
    query = WideHistoryQueryService(
        backend=backend,
        codec=WideCursorCodec(b"k" * 32),
    )
    with pytest.raises(TDengineSchemaConflictError, match="identity mismatch"):
        await query.query(
            schema=_schema(),
            filter_=_filter(quality=quality),
            device_type="wtg",
        )
    assert backend.reads == 0


def test_metadata_only_missing_registration_is_skippable(tmp_path: Path) -> None:
    with (
        MetadataStore(tmp_path / "metadata.sqlite") as metadata,
        pytest.raises(WideTableNotCreatedError),
    ):
        metadata.require_ready_wide_table(
            name="wh_farm_0000000000000000.tb_device_0000000000000000",
            fingerprint="f" * 64,
            identity_sha256="b" * 64,
        )
