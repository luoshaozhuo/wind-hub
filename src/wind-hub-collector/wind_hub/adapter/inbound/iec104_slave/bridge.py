"""IEC104 从站代理与采集/命令核心之间的桥接层。

本模块只适配两个方向：
1. 采集方向：作为 AcquisitionEngine observer，把最新 PointValue 写入
   DataSnapshot，供总召等从站请求读取；
2. 控制方向：把调度主站下发的遥控命令转交 CommandDispatcher。

桥接层不持有设备连接、不执行协议编码，也不改变 CommandDispatcher 的幂等、
超时和错误语义。
"""

from __future__ import annotations

from wind_hub.adapter.inbound.iec104_slave.buffer import DataSnapshot
from wind_hub.application.command_dispatcher import CommandDispatcher
from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.point import PointValue


class SlaveBridge:
    """把采集 observer 与命令派发器收敛为 IEC104 从站所需的最小接口。

    Args:
        dispatcher: Collector 的命令派发器。
        snapshot: IEC104 从站共享的最新值快照。
        mapping: (device_id, point_id) 到 IOA 的映射。
    """

    def __init__(
        self,
        dispatcher: CommandDispatcher,
        snapshot: DataSnapshot,
        mapping: dict[tuple[str, str], int],
    ) -> None:
        self._dispatcher = dispatcher
        self._snapshot = snapshot
        self._mapping = mapping

    def on_points_collected(self, values: list[PointValue]) -> None:
        """把本轮新采集值写入从站快照。

        Args:
            values: AcquisitionEngine 已完成处理的点值批次。
        """
        self._snapshot.update(values, self._mapping)

    async def forward_command(self, cmd: Command) -> CommandResult:
        """把远端控制命令交给 Collector 命令链执行。

        Args:
            cmd: 已解析为领域 Command 的遥控指令。

        Returns:
            CommandDispatcher 返回的执行结果。
        """
        return await self._dispatcher.send(cmd)
