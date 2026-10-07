"""Shared Domain 业务点模型。"""

from __future__ import annotations

from dataclasses import dataclass

from .identities import BusinessPointId
from .unit import Unit
from .value_objects import ValueType


@dataclass(frozen=True, slots=True)
class BusinessPoint:
    """稳定业务点实体，回答“这个量是什么”."""

    business_point_id: BusinessPointId
    value_type: ValueType
    standard_unit: Unit
    description: str | None = None

    def __post_init__(self) -> None:
        business_point_id = self.business_point_id.strip()
        if not business_point_id:
            raise ValueError("business_point_id must not be empty")
        object.__setattr__(
            self,
            "business_point_id",
            BusinessPointId(business_point_id),
        )
