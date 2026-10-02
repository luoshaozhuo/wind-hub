"""IEC104 从站代理的内存最新值快照。

快照以 IOA 为键保存最新 PointValue，用于总召等读取场景，避免请求到来时再次
访问现场设备。该对象只在 Collector 事件循环线程内使用，因此使用普通 dict，
不引入锁或额外异步资源。
"""

from __future__ import annotations

from wind_hub_core.model.point import PointValue


class DataSnapshot:
    """按 IEC104 IOA 保存最新 PointValue 的轻量缓存。

    只保存 reporting 配置暴露的点；不做历史存储、质量计算或持久化。
    """

    def __init__(self) -> None:
        self._snapshot: dict[int, PointValue] = {}

    def update(self, values: list[PointValue], mapping: dict[tuple[str, str], int]) -> None:
        """更新 reporting 范围内点位的最新值。

        Args:
            values: 新采集到的点值。
            mapping: (device_id, point_id) 到 IOA 的映射。未映射点会被忽略。
        """
        for pv in values:
            ioa = mapping.get((pv.device_id, pv.point_id))
            if ioa is None:
                continue
            self._snapshot[ioa] = pv

    def get(self, ioa: int) -> PointValue | None:
        """查询一个 IOA 的最新值。

        Returns:
            已缓存的 PointValue；尚无数据时返回 None。
        """
        return self._snapshot.get(ioa)

    def get_all(self) -> list[tuple[int, PointValue]]:
        """返回全部最新值，并按 IOA 升序排列。"""
        return sorted(self._snapshot.items(), key=lambda item: item[0])

    @property
    def size(self) -> int:
        """返回当前快照持有的 IOA 数量。"""
        return len(self._snapshot)
