"""Sink 外部引用导出。

本模块只负责将内部 PointValue 按 ResolvedSinkPoint 映射为外部引用值，并执行
Sink 层二次 scale/offset。协议编码、网络发送与背压不属于本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from wind_hub_core.config.sinks import ResolvedSinkPoint
from wind_hub_core.model.point import PointValue, Quality


@dataclass(frozen=True, slots=True)
class ExportedSinkPointValue:
    """一个已绑定外部引用的运行时点值。"""

    definition: ResolvedSinkPoint
    value: Any
    quality: Quality
    timestamp: datetime
    source_protocol: str | None

    @property
    def ref(self) -> str:
        return self.definition.ref

    @property
    def device_id(self) -> str:
        return self.definition.source.device_id

    @property
    def point_id(self) -> str:
        return self.definition.source.point_id


class SinkReferenceExporter:
    """按内部 (device_id, point_id) 将 PointValue 映射为外部 Sink 引用。"""

    def __init__(self, points: list[ResolvedSinkPoint]) -> None:
        mapping: dict[tuple[str, str], list[ResolvedSinkPoint]] = {}
        for point in points:
            key = (point.source.device_id, point.source.point_id)
            mapping.setdefault(key, []).append(point)
        self._mapping = mapping

    def export(self, batch: list[PointValue]) -> list[ExportedSinkPointValue]:
        """导出当前 Sink 显式定义的点；未映射输入点直接忽略。"""
        exported: list[ExportedSinkPointValue] = []
        for value in batch:
            definitions = self._mapping.get((value.device_id, value.point_id), ())
            for definition in definitions:
                exported.append(self._export_one(definition, value))
        return exported

    @staticmethod
    def _export_one(
        definition: ResolvedSinkPoint,
        value: PointValue,
    ) -> ExportedSinkPointValue:
        transformed = value.value
        if transformed is not None and (definition.scale != 1.0 or definition.offset != 0.0):
            if isinstance(transformed, bool) or not isinstance(transformed, int | float):
                raise TypeError(
                    f"Sink point '{definition.ref}' expects numeric value for scale/offset, "
                    f"got {type(transformed).__name__}"
                )
            transformed = transformed * definition.scale + definition.offset
        return ExportedSinkPointValue(
            definition=definition,
            value=transformed,
            quality=value.quality,
            timestamp=value.timestamp,
            source_protocol=value.source,
        )


__all__ = ["ExportedSinkPointValue", "SinkReferenceExporter"]
