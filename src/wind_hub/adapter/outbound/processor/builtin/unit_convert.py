"""内置处理器——单位换算（真实实现）。

对数值类型测点应用 ``value = value * scale + offset``，其中 ``scale`` /
``offset`` 来自点表（:class:`~wind_hub.config.schema.PointConfig`），由
:meth:`set_points_config` 在装配后注入。bool / string 类型跳过换算；单个
点的换算失败仅把该点 quality 标记为 BAD，不影响批量内其它点。
"""

from __future__ import annotations

from wind_hub.adapter.outbound.processor.builtin._util import is_numeric
from wind_hub.config.schema import NUMERIC_DATA_TYPES, PointConfig
from wind_hub.domain.model.point import PointValue, Quality
from wind_hub.domain.port.outbound import ProcessorPort
from wind_hub.infra.processor_registry import register_processor


class UnitConvertProcessor(ProcessorPort):
    """单位换算处理器（``value * scale + offset``）。

    无状态：``set_points_config`` 只在初始化时被组合根调用一次。
    """

    def __init__(self) -> None:
        self._scale_offset: dict[tuple[str, str], tuple[float, float]] = {}

    def set_points_config(self, points_by_device: dict[str, list[PointConfig]]) -> None:
        """注入点表配置，建立 ``(device_id, point_id) → (scale, offset)``。

        只收录数值类型 ``data_type`` 的点；bool / string 点不需要换算，
        因而不会进入映射（后续 ``process`` 对未映射点原样透传）。
        """
        self._scale_offset = {
            (device_id, p.point_id): (p.scale, p.offset)
            for device_id, points in points_by_device.items()
            for p in points
            if p.data_type in NUMERIC_DATA_TYPES
        }

    @property
    def name(self) -> str:
        """处理器唯一名，须与 ``system.yaml`` 的 ``pipeline.processors`` 一致。"""
        return "unit_convert"

    async def process(self, batch: list[PointValue]) -> list[PointValue]:
        """对每个点应用换算。

        未映射（非数值类型）或 ``scale == 1.0 and offset == 0.0`` 的点原样
        透传；映射内但值非数值、或算术换算失败的点 quality 标记为 BAD 且值
        不变；其余应用 ``value * scale + offset``。
        """
        result: list[PointValue] = []
        for pv in batch:
            conv = self._scale_offset.get((pv.device_id, pv.point_id))
            if conv is None:
                result.append(pv)
                continue
            scale, offset = conv
            if scale == 1.0 and offset == 0.0:
                result.append(pv)
                continue
            if not is_numeric(pv.value):
                result.append(pv.model_copy(update={"quality": Quality.BAD}))
                continue
            try:
                new_value = pv.value * scale + offset
            except (TypeError, ValueError, OverflowError):
                result.append(pv.model_copy(update={"quality": Quality.BAD}))
                continue
            result.append(pv.model_copy(update={"value": new_value}))
        return result


@register_processor("unit_convert")
def _create() -> ProcessorPort:
    return UnitConvertProcessor()
