"""Inbound ports — use-case interfaces that external clients call."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.device import DeviceInfo
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.model.reload import ReloadResult
from wind_hub.domain.model.route import RouteDecision
from wind_hub.domain.port.scheduling import JobInfo

if TYPE_CHECKING:
    from wind_hub.config.schema import Config


class AcquisitionInfo(BaseModel):
    """单个采集 Job（``(device, group)``）的业务执行状态快照。

    与调度器的 Job 状态（注册/暂停/下次触发时间）和设备连接状态分维度：
    本模型只描述「这个采集 Job 最近跑得怎样」。
    """

    device_id: str
    """Device identifier."""

    group: str
    """Polling group name."""

    running: bool = False
    """``True`` while a collect run is in flight."""

    consecutive_failures: int = 0
    """连续失败次数（partial 不算失败）。"""

    last_error: str | None = None
    """最近一次失败的简要描述。"""

    last_duration: float | None = None
    """最近一次 collect 耗时（秒，单调时钟口径）。"""


class SystemStatus(BaseModel):
    """Runtime status snapshot returned by :meth:`QueryUseCase.status`."""

    running: bool
    """``True`` when the engine loop is active."""

    device_count: int
    """Number of configured devices."""

    sink_count: int
    """Number of configured sinks."""

    devices_connected: int = 0
    """Number of devices currently reporting a healthy connection."""

    sinks_healthy: int = 0
    """Number of sinks currently reporting healthy."""

    points_collected: int = 0
    """累计采集点数（调度器单调计数，进程重启归零）。"""

    points_routed: int = 0
    """累计路由点数——成功进入 sink 队列的点值总数。"""

    points_dropped: int = 0
    """累计丢弃点数——背压策略丢弃的点值总数。"""

    acquisitions: list[AcquisitionInfo] = []
    """各采集 Job 的业务执行状态（按 ``(device, group)`` 粒度）。"""


class CommandUseCase(Protocol):
    """Command issuance use case — shared by CLI and Web API adapters."""

    async def send(self, cmd: Command) -> CommandResult:
        """Issue a single write command.

        Args:
            cmd: The command to execute.

        Returns:
            A ``CommandResult`` whose ``success`` field indicates
            whether the write was acknowledged.  Failures are
            reported inline — this method does **not** raise on
            protocol-level errors.

        Raises:
            CommandError: If the command could not be dispatched
                at all (e.g. unknown device).
        """
        ...

    async def send_batch(self, cmds: list[Command]) -> list[CommandResult]:
        """Issue multiple write commands.

        Every input ``Command`` produces exactly one result in the
        returned list, at the same index.

        Args:
            cmds: Commands to execute.

        Returns:
            One ``CommandResult`` per command, in input order.
        """
        ...


class JobUseCase(Protocol):
    """调度 Job 运行控制用例——只管「Scheduled Job 生命周期」。

    与另外两层启停语义严格区分：Runtime 整体 ``start()``/``stop()`` 属于
    Runtime 生命周期（由组合根/进程入口编排），进程启停属于 systemd /
    Docker / ``main.py``；本用例不涉及这两者。
    """

    async def list_jobs(self) -> list[JobInfo]:
        """Return snapshots of all scheduled jobs."""
        ...

    async def job_status(self, job_id: str) -> JobInfo:
        """Return the snapshot of a single job.

        Raises:
            KeyError: If the job is unknown.
        """
        ...

    async def pause_job(self, job_id: str) -> None:
        """Pause a job — its definition is kept but it stops firing.

        Raises:
            KeyError: If the job is unknown.
        """
        ...

    async def resume_job(self, job_id: str) -> None:
        """Resume a paused job.

        Raises:
            KeyError: If the job is unknown.
        """
        ...

    async def trigger_job(self, job_id: str) -> None:
        """Trigger one immediate run of a job; its schedule is unchanged.

        Raises:
            KeyError: If the job is unknown.
        """
        ...


class QueryUseCase(Protocol):
    """Read-only query use case."""

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        """Trigger an immediate real-time read of a single point.

        Args:
            device_id: Target device identifier.
            point_id: Point identifier within the device.

        Returns:
            The current value of the requested point.

        Raises:
            ProtocolError: If the device is unreachable or the read fails.
            CommandError: If the device or point is unknown.
        """
        ...

    async def list_devices(self) -> list[DeviceInfo]:
        """Return runtime status of all configured devices."""
        ...

    async def get_device_info(self, device_id: str) -> DeviceInfo:
        """Return runtime status of a single device.

        Args:
            device_id: Device identifier.

        Raises:
            CommandError: If the device is unknown.
        """
        ...

    async def status(self) -> SystemStatus:
        """Return a snapshot of the current system state.

        Reads through the runtime's live registries, so the snapshot
        always reflects the latest hot-reload state.
        """
        ...


class ConfigUseCase(Protocol):
    """Configuration use case — hot-reload and current-config access.

    说明：``current_config`` 返回 :class:`~wind_hub.config.schema.Config`，
    该类型定义在 config 层（依赖 domain），因此协议本体只通过
    ``TYPE_CHECKING`` 引用它，避免在运行时把 config 层耦合进 domain 端口。
    """

    @property
    def current_config(self) -> Config:
        """返回当前生效的配置快照（同步只读）。"""
        ...

    async def reload(self) -> ReloadResult:
        """触发一次完整的热重载。

        Returns:
            :class:`ReloadResult` 描述本次变更与是否成功；加载失败时
            不应用任何改动，``success`` 为 ``False`` 且 ``errors`` 非空。
        """
        ...


class RouteQueryUseCase(Protocol):
    """路由查询用例——针对点位的路由决策与未匹配点查询。

    路由表是纯内存结构、无 I/O，因此方法设计为同步，与 :class:`Router`
    的纯逻辑语义一致，避免引入不必要的 ``asyncio`` 边界。
    """

    def explain(self, device_id: str, point_id: str) -> RouteDecision:
        """解释某点位的路由决策。

        Args:
            device_id: 设备标识。
            point_id: 点位标识。

        Returns:
            :class:`RouteDecision`，其 ``source`` 为
            ``'point_override'`` / ``'rule'`` / ``'unmatched'`` 之一。
        """
        ...

    def unmatched_points(self) -> list[tuple[str, str]]:
        """返回所有没有路由目标的 ``(device_id, point_id)`` 键。"""
        ...
