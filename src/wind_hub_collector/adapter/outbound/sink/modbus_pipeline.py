"""Modbus Sink 数据路径。

负责把 Runtime 的 PointValue 批次依次经过 SinkReferenceExporter、Modbus 编码器
和 ModbusSinkStore。该层仍不创建 TCP Server。
"""

from __future__ import annotations

from wind_hub_collector.adapter.outbound.sink.modbus_codec import encode_modbus_value
from wind_hub_collector.adapter.outbound.sink.modbus_store import ModbusSinkStore
from wind_hub_collector.application.sink_export import SinkReferenceExporter
from wind_hub_core.config.sinks import ResolvedSinkPoint
from wind_hub_core.model.point import PointValue, Quality


class ModbusSinkDataPath:
    """Modbus Sink 的纯内存数据更新路径。"""

    def __init__(self, points: list[ResolvedSinkPoint]) -> None:
        self._exporter = SinkReferenceExporter(points)
        self._store = ModbusSinkStore(points)

    @property
    def store(self) -> ModbusSinkStore:
        """返回当前内存数据区。"""
        return self._store

    def update(self, batch: list[PointValue]) -> int:
        """把有效点值写入 datastore，返回实际更新的外部点数量。

        Modbus 寄存器本身没有统一的数据质量通道，因此 BAD/UNCERTAIN 或 None
        不覆盖最近一次 GOOD 值。
        """
        updated = 0
        for exported in self._exporter.export(batch):
            if exported.quality is not Quality.GOOD or exported.value is None:
                continue
            self._store.write(encode_modbus_value(exported))
            updated += 1
        return updated


__all__ = ["ModbusSinkDataPath"]
