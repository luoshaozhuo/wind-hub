"""内置处理器——死区过滤。

对数值类型且配置了 ``deadband`` 的测点进行死区过滤：新值与上次输出值之差
的绝对值小于死区阈值时不输出该点，否则输出并更新"上次输出值"。这是唯一
有状态的内置处理器（内部维护每个点的上次输出值）。
"""

from __future__ import annotations

from wind_hub.adapter.outbound.processor.builtin._util import is_numeric
from wind_hub.config.schema import NUMERIC_DATA_TYPES, PointConfig
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.port.outbound import ProcessorPort
from wind_hub.infra.processor_registry import register_processor


class DeadbandProcessor(ProcessorPort):
    """死区过滤处理器（有状态——维护每个点的上次输出值）。"""

    def __init__(self) -> None:
        self._deadbands: dict[tuple[str, str], float] = {}
        self._last_values: dict[tuple[str, str], float] = {}

    def set_points_config(self, points_by_device: dict[str, list[PointConfig]]) -> None:
        """注入点表配置，建立 ``(device_id, point_id) → deadband``。

        只收录数值类型且 ``deadband`` 非 ``None`` 的点；其余点不参与过滤。
        """
        self._deadbands = {
            (device_id, p.point_id): p.deadband
            for device_id, points in points_by_device.items()
            for p in points
            if p.deadband is not None and p.data_type in NUMERIC_DATA_TYPES
        }

    @property
    def name(self) -> str:
        """处理器唯一名，须与 ``system.yaml`` 的 ``pipeline.processors`` 一致。"""
        return "deadband"

    async def process(self, batch: list[PointValue]) -> list[PointValue]:
        """过滤死区内的点。

        未配置 deadband、或值非数值的点原样输出；首次遇见某点输出并记录；
        值变化绝对值 ``< deadband`` 时丢弃该点；否则输出并更新状态。
        """
        result: list[PointValue] = []
        for pv in batch:
            key = (pv.device_id, pv.point_id)
            deadband = self._deadbands.get(key)
            if deadband is None:
                result.append(pv)
                continue
            if not is_numeric(pv.value):
                result.append(pv)
                continue
            value = float(pv.value)
            last = self._last_values.get(key)
            if last is None:
                # 第一次见到该点：输出并记录，不做过滤。
                self._last_values[key] = value
                result.append(pv)
            elif abs(value - last) >= deadband:
                self._last_values[key] = value
                result.append(pv)
            # 否则在死区内，丢弃该点。
        return result

    def reset(self) -> None:
        """清空内部状态（供测试或热加载替换 Pipeline 后重置使用）。"""
        self._last_values.clear()


@register_processor("deadband")
def _create() -> ProcessorPort:
    return DeadbandProcessor()
