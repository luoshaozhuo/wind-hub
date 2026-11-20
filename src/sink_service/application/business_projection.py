"""Pure historical business standardization using a pinned PointTable."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from core.domain import BusinessPoint, BusinessPointId, PointTable
from core.domain.point import engineering_value
from core.domain.unit import convert_value
from sink_service.domain.config import ConfigurationError


@dataclass(frozen=True, slots=True)
class BusinessValue:
    """One verified business-standard value, retaining its source identity."""

    business_point_id: str
    unit: str
    value: float | int | bool | str | None


@dataclass(frozen=True, slots=True)
class HistoricalPointTable:
    """Verified immutable semantics for exactly one source PointTable identity."""

    point_table_id: str
    device_type: str
    manifest_sha256: str
    table: PointTable
    business_points: dict[BusinessPointId, BusinessPoint]

    def project(self, point_id: str, value: float | int | bool | str | None) -> BusinessValue:
        point = self.table.points.get(point_id)
        if point is None:
            raise ConfigurationError(f"unknown point in historical PointTable: {point_id}")
        business = self.business_points.get(point.business_point_id)
        if business is None:
            raise ConfigurationError("missing pinned BusinessPoint")
        if value is None:
            return BusinessValue(
                str(business.business_point_id), business.standard_unit.unit_id, None
            )
        try:
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and point.scale == 1.0
                and point.offset == 0.0
                and point.source_unit == business.standard_unit
            ):
                # Avoid float round-trip for exact int64/uint64 protocol values.
                converted: object = value
            else:
                engineering = engineering_value(point, value)
                if isinstance(engineering, int | float) and not isinstance(engineering, bool):
                    if not isfinite(float(engineering)):
                        raise ValueError("nonfinite engineering value")
                    if isinstance(engineering, int) and abs(engineering) > 2**53:
                        raise ValueError("large integer cannot be converted losslessly")
                    converted = convert_value(
                        float(engineering), point.source_unit, business.standard_unit
                    )
                else:
                    converted = engineering
            result = business.coerce(converted)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ConfigurationError(f"business conversion failed for {point_id}: {exc}") from exc
        return BusinessValue(
            str(business.business_point_id), business.standard_unit.unit_id, result
        )
