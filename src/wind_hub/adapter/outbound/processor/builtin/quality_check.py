"""内置处理器——质量校验（值域校验）。

对数值类型且配置了 ``min_value`` / ``max_value`` 的测点做值域校验：读数低于
下限或高于上限时把 quality 标记为 BAD，值保持不变；未配置、非数值读数的点
原样透传。只改变 quality，不改值。
"""

from __future__ import annotations

from wind_hub.adapter.outbound.processor.builtin._util import is_numeric
from wind_hub.config.schema import NUMERIC_DATA_TYPES, PointConfig
from wind_hub.domain.model.point import PointValue, Quality
from wind_hub.domain.port.outbound import ProcessorPort
from wind_hub.infra.processor_registry import register_processor


class QualityCheckProcessor(ProcessorPort):
    """质量校验处理器——越界值标记为 BAD（无状态）。"""

    def __init__(self) -> None:
        self._ranges: dict[tuple[str, str], tuple[float | None, float | None]] = {}

    def set_points_config(self, points_by_device: dict[str, list[PointConfig]]) -> None:
        """注入点表配置，建立 ``(device_id, point_id) → (min, max)``。

        只收录数值类型且至少配置了一个边界的点；无约束的点不参与校验。
        """
        self._ranges = {
            (device_id, p.point_id): (p.min_value, p.max_value)
            for device_id, points in points_by_device.items()
            for p in points
            if p.data_type in NUMERIC_DATA_TYPES
            and (p.min_value is not None or p.max_value is not None)
        }

    @property
    def name(self) -> str:
        """处理器唯一名，须与 ``system.yaml`` 的 ``pipeline.processors`` 一致。"""
        return "quality_check"

    async def process(self, batch: list[PointValue]) -> list[PointValue]:
        """校验每个点的值域。

        未映射、或值非数值的点原样透传；越界（低于 min 或高于 max）的点
        quality 标记为 BAD 且值不变；在范围内则保持原 quality 不变。
        """
        result: list[PointValue] = []
        for pv in batch:
            bounds = self._ranges.get((pv.device_id, pv.point_id))
            if bounds is None:
                result.append(pv)
                continue
            if not is_numeric(pv.value):
                result.append(pv)
                continue
            low, high = bounds
            value = float(pv.value)
            if (low is not None and value < low) or (high is not None and value > high):
                result.append(pv.model_copy(update={"quality": Quality.BAD}))
            else:
                result.append(pv)
        return result


@register_processor("quality_check")
def _create() -> ProcessorPort:
    return QualityCheckProcessor()
