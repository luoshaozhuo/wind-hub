"""采集选点配置。"""

from __future__ import annotations

from dataclasses import dataclass

from core.domain import BusinessPointId

from .identities import PointSetId


@dataclass(frozen=True, slots=True)
class PointSet:
    """可复用的业务点集合。

    PointSet 属于配置语义，不属于 Domain PointTable。它描述采集任务希望处理
    哪些稳定业务变量；具体设备上的 ProtocolPoint 由
    Device -> DeviceModel -> PointTable 再按 business_point_id 解析。

    同一业务点在同一 PointTable 中存在多个 ProtocolPoint 时，解析结果可以包含
    多个协议点；PointSet 本身不关心协议地址与协议类型。
    """

    point_set_id: PointSetId
    business_point_ids: tuple[BusinessPointId, ...]

    def __post_init__(self) -> None:
        point_set_id = self.point_set_id.strip()
        if not point_set_id:
            raise ValueError("point_set_id must not be empty")

        business_point_ids = tuple(
            BusinessPointId(point_id.strip()) for point_id in self.business_point_ids
        )
        if not business_point_ids:
            raise ValueError("business_point_ids must not be empty")
        if any(not point_id for point_id in business_point_ids):
            raise ValueError("business_point_ids must not contain empty values")
        if len(business_point_ids) != len(set(business_point_ids)):
            raise ValueError("business_point_ids must not contain duplicates")

        object.__setattr__(self, "point_set_id", PointSetId(point_set_id))
        object.__setattr__(self, "business_point_ids", business_point_ids)
