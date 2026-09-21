"""CollectionTaskInstance —— Runtime 针对具体设备展开的采集执行实例。

架构位置：application/runtime。配置层 :class:`CollectionTaskConfig`
（Task Definition）描述「采哪个设备/设备组、哪个 point group、周期、输出
到哪些 sink」；本模型是它在运行时的具体化（Task Instance）——

- ``device`` Task：一个定义展开为一个实例；
- ``device_group`` Task：每台命中的 enabled 设备展开为一个实例。

实例 ID 稳定且唯一：``{task_id}:{device_id}``。实例是**不可变快照**——
热重载修改 interval / targets / point_group 时整体替换实例对象，运行中的
实例采集协程每轮从 Runtime 注册表读取最新实例，无需重启协程即可生效。

与两个相邻概念严格分维度：

- :class:`AcquisitionRuntimeState`——业务执行状态（最近一次 collect 成功/
  失败/耗时），以 ``instance_id`` 为键；
- 实例生命周期状态（RUNNING / STOPPED）——由 Runtime 以
  ``{instance_id: bool}`` 显式簿记（见 ``Runtime.instance_states``），
  决定实例采集协程是否存在。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class TaskInstanceState(str, Enum):
    """Task Instance 的生命周期状态——只有两个取值。

    与采集执行状态（failed/partial）和设备连接状态（disconnected）严格
    分维度：采集失败或设备掉线不改变实例的启停状态。
    """

    RUNNING = "running"
    """实例采集协程存在——按「collect → sleep(interval)」循环执行。"""

    STOPPED = "stopped"
    """实例已注册但协程不存在——保留定义，不执行采集。"""


class CollectionTaskInstance(BaseModel):
    """Runtime 针对具体设备展开的采集执行实例（不可变快照）。"""

    model_config = ConfigDict(frozen=True)

    instance_id: str
    """稳定唯一标识：``{task_id}:{device_id}``。"""

    task_id: str
    """来源 Task Definition（``tasks.yaml`` 的 ``task_id``）。"""

    device_id: str
    """本实例采集的具体设备。"""

    point_group: str
    """采集的点位分组（匹配 ``PointConfig.point_groups``）。"""

    interval: float
    """两轮采集之间的等待时间（秒）。"""

    targets: list[str]
    """输出目标 Sink 名列表（引用 ``system.yaml`` 的 sink 定义）。"""


def task_instance_id(task_id: str, device_id: str) -> str:
    """Task Instance 标识约定：``{task_id}:{device_id}``。"""
    return f"{task_id}:{device_id}"
