"""IEC104 reporting 从站与采集数据快照之间的桥接层。"""

from __future__ import annotations

from wind_hub_collector.adapter.inbound.iec104_slave.buffer import DataSnapshot
from wind_hub_core.model.point import PointValue


class SlaveBridge:
    """把采集结果写入 IEC104 reporting 最新值快照。"""

    def __init__(
        self,
        snapshot: DataSnapshot,
        mapping: dict[tuple[str, str], int],
    ) -> None:
        self._snapshot = snapshot
        self._mapping = mapping

    def on_points_collected(self, values: list[PointValue]) -> None:
        """把本轮新采集值写入从站快照。"""
        self._snapshot.update(values, self._mapping)
