"""IEC104 Sink 的最新值快照。

快照直接保存已经完成 source/ref/scale/offset 解析的 ExportedSinkPointValue，
以 IOA 为键供 IEC104 总召读取。这里不再维护独立的 reporting 映射表。
"""

from __future__ import annotations

from wind_hub_collector.application.sink_export import ExportedSinkPointValue
from wind_hub_core.config import IEC104SinkAddress


class DataSnapshot:
    """按 IEC104 IOA 保存最新导出点值。"""

    def __init__(self) -> None:
        self._snapshot: dict[int, ExportedSinkPointValue] = {}

    def update(self, values: list[ExportedSinkPointValue]) -> None:
        """更新当前 Sink 已解析点的最新值。"""
        for value in values:
            address = value.definition.address
            if not isinstance(address, IEC104SinkAddress):
                continue
            self._snapshot[address.ioa] = value

    def get(self, ioa: int) -> ExportedSinkPointValue | None:
        """查询一个 IOA 的最新值；尚无数据时返回 None。"""
        return self._snapshot.get(ioa)

    def get_all(self) -> list[tuple[int, ExportedSinkPointValue]]:
        """返回全部最新值，并按 IOA 升序排列。"""
        return sorted(self._snapshot.items(), key=lambda item: item[0])

    @property
    def size(self) -> int:
        """返回当前快照持有的 IOA 数量。"""
        return len(self._snapshot)
