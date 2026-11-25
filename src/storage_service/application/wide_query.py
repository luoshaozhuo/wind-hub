"""Version-scoped, device-level wide-table history query contracts."""

from __future__ import annotations

from dataclasses import dataclass

from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import WideSchema


@dataclass(frozen=True, slots=True)
class WideHistoryFilter:
    wind_farm_id: str
    device_id: str
    device_config_id: str
    data_level: WideDataLevel
    start_time_us: int
    end_time_us: int
    point_ids: tuple[str, ...]
    limit: int
    after_time_us: int | None = None

    def validate(self, schema: WideSchema, *, maximum: int) -> None:
        if (
            not self.wind_farm_id or not self.device_id or not self.device_config_id
            or self.data_level != schema.data_level
        ):
            raise ValueError("wide query requires an exact device version and data level")
        if (
            type(self.start_time_us) is not int or type(self.end_time_us) is not int
            or self.start_time_us >= self.end_time_us
        ):
            raise ValueError("wide query time range must be nonempty")
        if type(self.limit) is not int or not 1 <= self.limit <= maximum:
            raise ValueError("wide query limit exceeds configured maximum")
        if (
            self.after_time_us is not None
            and (type(self.after_time_us) is not int or self.after_time_us < self.start_time_us)
        ):
            raise ValueError("wide query pagination cursor is invalid")
        actual = {column.point_id for column in schema.columns}
        if not self.point_ids or len(set(self.point_ids)) != len(self.point_ids):
            raise ValueError("wide query field set must be unique and nonempty")
        if not set(self.point_ids).issubset(actual):
            raise ValueError("wide query selects fields outside the exact physical schema")


@dataclass(frozen=True, slots=True)
class WideHistoryRow:
    sample_time_us: int
    points: tuple[tuple[str, object, PointQuality], ...]
