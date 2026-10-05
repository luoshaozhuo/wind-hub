"""IEC 60870-5-104 ProtocolPort 实现（基于 c104/lib60870-C）。

wind-hub 不再自行实现 APCI/ASDU 编解码、I/S/U 帧状态机、序号管理、k/w
流控和 t1/t2/t3 timer——这些全部由 c104/lib60870-C 承担，包括断线自动
重连。本模块只负责：

- DeviceConfig → c104 Client/Connection/Station 的运行时映射；
- c104 回调线程 → asyncio 事件循环的安全桥接；
- 领域 Command / PointValue 与 c104 类型的转换（见 mapping.py）；
- 订阅注册与 PointValue 分发。

read() 读取 c104 客户端镜像点的最新值，不为每次调用重新发总召；write()
把领域 Command 转换为控制点 transmit，并按 c104 的确认结果解析
CommandResult。c104 客户端在断线后自动重连，恢复 OPEN 且有活动订阅时
补发一次总召刷新镜像。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from wind_hub_core.config import DeviceConfig, PointConfig
from wind_hub_core.model.command import Command, CommandResult
from wind_hub_core.model.errors import ConfigError, ProtocolError
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointRef, PointValue, Quality
from wind_hub_core.protocol.iec104.config import IEC104Config
from wind_hub_core.protocol.iec104.mapping import (
    command_to_c104,
    point_value_from_c104,
)
from wind_hub_core.protocol.port import (
    AcquisitionMode,
    ProtocolPort,
    SubscriptionHandle,
)

if TYPE_CHECKING:
    import c104

    from wind_hub_core.protocol.iec104.callbacks import ReceiveCallbackFactory
else:  # pragma: no cover - 依赖存在性由构造时守卫
    try:
        import c104
    except ImportError:
        c104 = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _seconds_to_int(value: float, name: str) -> int:
    """把秒级浮点配置转换为 c104 要求的 int 秒；不足 1 秒的配置拒绝。"""
    result = int(value)
    if result < 1:
        raise ConfigError(f"IEC104: '{name}' must be >= 1s for c104, got {value}")
    return result


class _Subscription:
    """一次订阅的句柄——close 只注销本次订阅，不影响同设备的其他订阅。

    close 契约：返回后本订阅的 callback 不再开始新的调用，且已开始的调用
    都已执行完毕（in-flight drain barrier）。快照与注册表操作都发生在事件
    循环线程，因此注销与分发之间不存在交错。
    """

    def __init__(
        self,
        registry: _SubscriptionRegistry,
        callback: Callable[[PointValue], Awaitable[None]],
        ioas: list[int] | None,
    ) -> None:
        self._registry = registry
        self._callback = callback
        self._ioas = ioas
        self._closed = False
        self._in_flight = 0
        self._idle: asyncio.Event | None = None

    async def close(self) -> None:
        """注销本次订阅（幂等）——移除回调并等待在途调用完成，不触碰连接。"""
        if self._closed:
            return
        self._closed = True
        self._registry.unsubscribe(self)
        # 等待 close 前已分发的 callback 调用完成；此后注册表不再持有本句柄，
        # 不会有新调用入队。
        while self._in_flight:
            self._idle = asyncio.Event()
            await self._idle.wait()
            self._idle = None

    def _track(self) -> bool:
        """分发前登记一次在途调用；已关闭时返回 False 不分发。"""
        if self._closed:
            return False
        self._in_flight += 1
        return True

    def _untrack(self) -> None:
        """一次在途调用完成；close 等待中时唤醒 drain。"""
        self._in_flight -= 1
        if self._in_flight == 0 and self._idle is not None:
            self._idle.set()


class _SubscriptionRegistry:
    """全局 + 按 IOA 的订阅注册表（仅在事件循环线程访问）。"""

    def __init__(self) -> None:
        self._global: list[_Subscription] = []
        self._ioa: dict[int, list[_Subscription]] = {}

    def subscribe(
        self,
        points: list[PointRef],
        callback: Callable[[PointValue], Awaitable[None]],
        ioa_resolver: Callable[[PointRef], int | None],
    ) -> _Subscription:
        """注册订阅并返回独立句柄；空 points 表示接收全部 PointValue。"""
        if not points:
            subscription = _Subscription(self, callback, None)
            self._global.append(subscription)
            return subscription

        ioas: list[int] = []
        for ref in points:
            ioa = ioa_resolver(ref)
            if ioa is None:
                logger.warning(
                    "IEC104: subscribe: unknown point '%s' — skipping",
                    ref.point_id,
                )
                continue
            ioas.append(ioa)
        subscription = _Subscription(self, callback, ioas)
        for ioa in ioas:
            self._ioa.setdefault(ioa, []).append(subscription)
        return subscription

    def unsubscribe(self, subscription: _Subscription) -> None:
        """移除一个全局或按 IOA 的订阅注册。"""
        if subscription._ioas is None:
            with contextlib.suppress(ValueError):
                self._global.remove(subscription)
            return
        for ioa in subscription._ioas:
            subscriptions = self._ioa.get(ioa)
            if subscriptions is None:
                continue
            with contextlib.suppress(ValueError):
                subscriptions.remove(subscription)
            if not subscriptions:
                del self._ioa[ioa]

    def clear(self) -> None:
        """清空全部订阅并标记关闭；用于 Driver 整体关闭。

        Driver 关闭后到达的 dispatch 不再向任何订阅投递；已在途的 callback
        调用允许完成（Driver 生命周期由 CollectorRuntime 统一编排）。
        """
        for subscription in self._global:
            subscription._closed = True
        for subscriptions in self._ioa.values():
            for subscription in subscriptions:
                subscription._closed = True
        self._global.clear()
        self._ioa.clear()

    @property
    def has_subscriptions(self) -> bool:
        """存在任何活动订阅时为 True。"""
        return bool(self._global) or bool(self._ioa)

    async def dispatch(self, pv: PointValue, ioa: int) -> None:
        """把 PointValue 分发给全部匹配订阅者；每个 callback 独立 task。

        快照与 in-flight 登记在同一事件循环临界区内完成：close 与 dispatch
        不会交错——先 close 则订阅不在快照中，先 dispatch 则 close 会等待
        本次调用完成。
        """
        subscriptions = [
            sub
            for sub in [*self._global, *self._ioa.get(ioa, [])]
            if sub._track()
        ]
        for sub in subscriptions:
            asyncio.ensure_future(self._invoke(sub, pv, ioa))

    async def _invoke(
        self,
        subscription: _Subscription,
        pv: PointValue,
        ioa: int,
    ) -> None:
        """调用单个 callback 并隔离异常，避免破坏分发循环。"""
        try:
            await subscription._callback(pv)
        except Exception:
            logger.exception(
                "IEC104: subscriber callback raised (IOA %d, point '%s')",
                ioa,
                pv.point_id,
            )
        finally:
            subscription._untrack()


class IEC104Driver:
    """单设备 IEC104 协议驱动（c104 客户端）。

    Args:
        cfg: 已解析的设备配置。

    Notes:
        Driver 由单个 asyncio event loop 持有。c104 客户端在自己的线程中
        运行协议状态机并自动重连；所有 c104 回调都运行在非 asyncio 线程，
        经 ``loop.call_soon_threadsafe`` 桥接进事件循环。
    """

    def __init__(self, cfg: DeviceConfig) -> None:
        if c104 is None:
            raise ConfigError(
                "IEC104 支持需要可选依赖 c104（安装 extras 'iec104' 后重试）"
            )
        self._config = IEC104Config.from_device_config(cfg)
        self._lock = asyncio.Lock()

        self._client: c104.Client | None = None
        self._connection: c104.Connection | None = None
        self._station: c104.Station | None = None

        # 事件循环绑定状态，connect() 时建立。
        self._loop: asyncio.AbstractEventLoop | None = None
        self._open_event: asyncio.Event | None = None
        self._is_open = False
        self._ever_connected = False
        self._closed = True

        # IOA/point_id 双向映射与点数据类型。
        self._ioa_to_point_id: dict[int, str] = {}
        self._point_id_to_ioa: dict[str, int] = {}
        self._point_data_types: dict[str, str] = {}

        # 本连接周期内已收到数据的 IOA；断线时清空，保证 read 不返回陈旧值。
        self._received_ioas: set[int] = set()

        # 同 IOA 命令串行化（响应才能稳定关联）。
        self._command_locks: dict[int, asyncio.Lock] = {}

        # c104 回调工厂，connect() 时延迟加载（callbacks 模块顶层 import
        # c104，不能在无 c104 环境于 import 期引入）；供新点回调内挂
        # on_receive 使用。
        self._receive_callback_factory: ReceiveCallbackFactory | None = None

        self._subscriptions = _SubscriptionRegistry()

    # ==================================================================
    # 点表映射
    # ==================================================================

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """构建设备 IOA/point_id 双向映射。

        缺失或非法 IOA 的点记录 warning 并跳过；重复 IOA 后者覆盖前者。
        """
        ioa_to_point_id: dict[int, str] = {}
        point_id_to_ioa: dict[str, int] = {}

        for p in points:
            try:
                # IEC104 PointAddress 的 ioa 来自协议扩展字段；
                # schema 若改为判别联合类型，应移除此抑制。
                ioa = int(p.address.ioa)  # type: ignore[attr-defined]
            except (AttributeError, ValueError):
                logger.warning(
                    "IEC104: point '%s' has no valid IOA — skipping",
                    p.point_id,
                )
                continue

            if ioa in ioa_to_point_id:
                logger.warning(
                    "IEC104: duplicate IOA %d for points '%s' and '%s'",
                    ioa,
                    ioa_to_point_id[ioa],
                    p.point_id,
                )
            ioa_to_point_id[ioa] = p.point_id
            point_id_to_ioa[p.point_id] = ioa

        self._ioa_to_point_id = ioa_to_point_id
        self._point_id_to_ioa = point_id_to_ioa
        self._point_data_types = {p.point_id: p.data_type for p in points}
        logger.info(
            "IEC104: mapped %d points for device %s",
            len(ioa_to_point_id),
            self._config.host,
        )

    def _resolve_ioa(self, ref: PointRef) -> int | None:
        """把 PointRef 解析为 IOA；未知点返回 None。"""
        return self._point_id_to_ioa.get(ref.point_id)

    # ==================================================================
    # ProtocolPort：连接生命周期
    # ==================================================================

    async def connect(self) -> None:
        """创建 c104 客户端、建连并等待数据传输就绪（OPEN）。

        建连成功后按既有契约做一次启动总召（best-effort，失败只记
        warning）；此后断线恢复由 c104 客户端自动完成。

        Raises:
            ProtocolError: t0 内未能进入 OPEN 状态。
        """
        async with self._lock:
            if self._client is not None:
                logger.warning("IEC104: connect() called but already connected")
                return
            self._closed = False
            self._loop = asyncio.get_running_loop()
            self._open_event = asyncio.Event()

            client = c104.Client(
                command_timeout_ms=int(self._config.t1 * 2 * 1000)
            )
            connection = client.add_connection(
                ip=self._config.host,
                port=self._config.port,
                init=c104.Init.NONE,
            )
            if connection is None:
                raise ProtocolError(
                    f"IEC104: invalid endpoint {self._config.host}:{self._config.port}"
                )
            self._apply_protocol_parameters(connection)
            station = connection.add_station(common_address=self._config.common_addr)
            if station is None:
                raise ProtocolError(
                    f"IEC104: invalid common_addr={self._config.common_addr}"
                )

            # c104 校验回调的精确类型注解；本模块启用 future annotations
            # 会退化为字符串，故经 callbacks 工厂（无 future import、延迟
            # 导入）生成带真实 c104 类型注解的回调。
            from wind_hub_core.protocol.iec104.callbacks import (
                new_point_callback,
                receive_callback,
                state_callback,
            )

            connection.on_state_change(
                callable=state_callback(self._handle_state_change)
            )
            client.on_new_point(
                callable=new_point_callback(self._handle_new_point)
            )
            self._receive_callback_factory = receive_callback

            loop = self._loop
            await loop.run_in_executor(None, client.start)
            connection.connect()

            try:
                await asyncio.wait_for(self._open_event.wait(), timeout=self._config.t0)
            except TimeoutError:
                await loop.run_in_executor(None, client.stop)
                self._closed = True
                raise ProtocolError(
                    f"IEC104: connect to {self._config.host}:{self._config.port} "
                    f"timed out after {self._config.t0}s"
                ) from None

            self._client = client
            self._connection = connection
            self._station = station
            self._is_open = True
            self._ever_connected = True

        # 启动总召是既有契约的一部分（旧实现 session.start 内完成）；
        # 失败不阻断 connect，后续 GI 由订阅方或显式 interrogate 触发。
        try:
            await self._interrogate()
        except ProtocolError:
            logger.warning(
                "IEC104: startup interrogation failed for %s:%d",
                self._config.host,
                self._config.port,
                exc_info=True,
            )

    async def close(self) -> None:
        """释放 c104 客户端与全部订阅；幂等。

        先断开连接再停止客户端线程；回调在停止后到达时由 ``_closed``
        守卫丢弃。close 不等待在途命令——c104 会令未确认的 transmit
        以失败返回。
        """
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._is_open = False

            client, connection = self._client, self._connection
            self._client = None
            self._connection = None
            self._station = None
            self._open_event = None
            self._receive_callback_factory = None
            self._received_ioas.clear()

            loop = self._loop
            if loop is not None:
                if connection is not None:
                    with contextlib.suppress(Exception):
                        await loop.run_in_executor(None, connection.disconnect)
                if client is not None:
                    with contextlib.suppress(Exception):
                        await loop.run_in_executor(None, client.stop)
            self._loop = None

            self._subscriptions.clear()

    def _apply_protocol_parameters(self, connection: c104.Connection) -> None:
        """把 t0/t1/t2/t3/k/w 配置写入 c104 连接参数。"""
        params = connection.protocol_parameters
        cfg = self._config
        params.connection_timeout = _seconds_to_int(cfg.t0, "t0")
        params.message_timeout = _seconds_to_int(cfg.t1, "t1")
        params.confirm_interval = _seconds_to_int(cfg.t2, "t2")
        params.keep_alive_interval = _seconds_to_int(cfg.t3, "t3")
        params.send_window_size = cfg.k
        params.receive_window_size = cfg.w

    # ==================================================================
    # ProtocolPort：读取
    # ==================================================================

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """读取 c104 客户端镜像点的最新值。

        未知点或本连接周期内尚未收到数据的点返回 Quality.BAD；读取不触发
        任何 wire 请求，数据新鲜度由总召与自发上送保证。

        Raises:
            ProtocolError: 驱动未连接。
        """
        station = self._station
        if self._closed or station is None or not self._is_open:
            raise ProtocolError("IEC104: cannot read — driver is not connected")

        results: list[PointValue] = []
        for ref in points:
            ioa = self._point_id_to_ioa.get(ref.point_id)
            point = station.get_point(ioa) if ioa is not None else None
            if ioa is None or ioa not in self._received_ioas or point is None:
                results.append(
                    PointValue(
                        device_id=ref.device_id,
                        point_id=ref.point_id,
                        value=None,
                        quality=Quality.BAD,
                        source="iec104",
                    )
                )
            else:
                pv = point_value_from_c104(point, ref.point_id)
                pv.device_id = ref.device_id
                results.append(pv)
        return results

    # ==================================================================
    # ProtocolPort：写入
    # ==================================================================

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """并发执行 IEC104 遥控命令。

        单条命令失败（未知点、类型冲突、否定确认、超时、断线）收敛为
        CommandResult.success=False，不取消其他命令。

        Raises:
            ProtocolError: 驱动未连接。
        """
        if not cmds:
            return []

        if self._closed or self._station is None or not self._is_open:
            raise ProtocolError("IEC104: cannot write — driver is not connected")

        tasks = [self._execute_one_command(cmd) for cmd in cmds]
        return await asyncio.gather(*tasks)

    async def _execute_one_command(self, cmd: Command) -> CommandResult:
        """执行单条遥控：映射 → 控制点 → transmit → 确认结果解析。"""
        try:
            ioa = self._point_id_to_ioa.get(cmd.point_id)
            if ioa is None:
                return CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error=f"unknown point '{cmd.point_id}'",
                )

            # c104 transmit 阻塞等待 ACT_CON（最长 command_timeout），
            # 必须离开事件循环；点创建、赋值与发送整体在同 IOA 锁内串行，
            # 保证发送的是本命令的值且响应能稳定关联。
            lock = self._command_locks.setdefault(ioa, asyncio.Lock())
            async with lock:
                try:
                    point_type, value = command_to_c104(
                        cmd, self._point_data_types.get(cmd.point_id, "")
                    )
                    point = self._get_or_create_command_point(ioa, point_type)
                except (ValueError, ProtocolError) as exc:
                    return CommandResult(
                        command_id=cmd.command_id,
                        success=False,
                        error=str(exc),
                    )

                point.value = value
                started = time.monotonic()
                loop = asyncio.get_running_loop()
                try:
                    accepted = await loop.run_in_executor(
                        None, point.transmit, c104.Cot.ACTIVATION
                    )
                except Exception as exc:
                    return CommandResult(
                        command_id=cmd.command_id,
                        success=False,
                        error=str(exc),
                    )
                elapsed = time.monotonic() - started

            if accepted:
                return CommandResult(command_id=cmd.command_id, success=True)

            return CommandResult(
                command_id=cmd.command_id,
                success=False,
                error=self._command_failure_reason(elapsed),
            )
        except Exception as exc:
            return CommandResult(
                command_id=cmd.command_id,
                success=False,
                error=str(exc),
            )

    def _command_failure_reason(self, elapsed: float) -> str:
        """把 transmit=False 归因为断线、超时或否定确认。"""
        if not self._is_open:
            return "connection lost"
        if elapsed >= self._config.t1 * 2 * 0.9:
            return "timeout"
        return "negative confirmation"

    def _get_or_create_command_point(
        self, ioa: int, point_type: c104.Type
    ) -> c104.Point:
        """获取或创建客户端控制点。

        c104 Station 以 IOA 为唯一键：同 IOA 已注册为其他类型（例如监视
        点镜像）时无法复用，属于点表配置冲突。

        Raises:
            ProtocolError: IOA 类型冲突或点创建失败。
        """
        station = self._station
        if station is None:
            raise ProtocolError("IEC104: driver is not connected")
        existing = station.get_point(ioa)
        if existing is not None:
            if existing.type == point_type:
                return existing
            raise ProtocolError(
                f"IEC104: IOA {ioa} already registered as "
                f"{existing.type.name}, cannot send {point_type.name} — "
                "point table conflict"
            )
        point = station.add_point(io_address=ioa, type=point_type)
        if point is None:
            raise ProtocolError(f"IEC104: cannot register command point at IOA {ioa}")
        return point

    # ==================================================================
    # ProtocolPort — subscribe / interrogate
    # ==================================================================

    @property
    def acquisition_mode(self) -> AcquisitionMode:
        """IEC104 天然接收 spontaneous / periodic / interrogation 数据——订阅式。"""
        return AcquisitionMode.SUBSCRIBE

    async def subscribe(
        self,
        points: list[PointRef],
        callback: Callable[[PointValue], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        """注册订阅并返回独立句柄。

        数据到达时机由远端决定（spontaneous / periodic / interrogation
        response），``interval`` 对 IEC104 无调度意义，仅透传忽略。
        关闭句柄只注销本次订阅，不关闭设备连接；断线重连由 c104 自动
        完成，注册表保留，数据流自然恢复。
        """
        return self._subscriptions.subscribe(points, callback, self._resolve_ioa)

    async def interrogate(self) -> None:
        """发送一次 General Interrogation（C_IC_NA_1，QOI=20，master 侧）。

        总召响应经既有点更新链进入各订阅回调，不另设返回通道。

        Raises:
            ProtocolError: 驱动未连接或总召未被接受。
        """
        await self._interrogate()

    async def _interrogate(self) -> None:
        connection = self._connection
        if self._closed or connection is None or not self._is_open:
            raise ProtocolError("IEC104: cannot interrogate — driver is not connected")
        loop = asyncio.get_running_loop()
        accepted = await loop.run_in_executor(
            None,
            lambda: connection.interrogation(
                common_address=self._config.common_addr,
                cause=c104.Cot.ACTIVATION,
                qualifier=c104.Qoi.STATION,
            ),
        )
        if not accepted:
            raise ProtocolError(
                f"IEC104: general interrogation rejected by {self._config.host}"
            )
        logger.info(
            "IEC104: general interrogation sent to %s:%d",
            self._config.host,
            self._config.port,
        )

    # ==================================================================
    # ProtocolPort — health
    # ==================================================================

    def health(self) -> HealthStatus:
        """返回当前缓存的 IEC104 连接健康状态；该同步接口不主动执行网络探测。"""
        if self._is_open:
            return HealthStatus(
                healthy=True,
                message=f"connected to {self._config.host}:{self._config.port}",
            )
        if self._client is None:
            return HealthStatus(healthy=False, message="not connected")
        return HealthStatus(healthy=False, message="connection not open")

    # ==================================================================
    # c104 回调（非 asyncio 线程）→ 事件循环桥接
    # ==================================================================

    def _handle_state_change(self, state: c104.ConnectionState) -> None:
        """c104 连接状态回调（c104 线程，经 callbacks 工厂收敛签名）；
        桥接进事件循环。"""
        loop = self._loop
        if loop is None:
            return
        # loop 已关闭时 call_soon_threadsafe 抛 RuntimeError：停机竞态，丢弃。
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._handle_state, state)

    def _handle_state(self, state: c104.ConnectionState) -> None:
        """在事件循环内应用连接状态，并触发重连后的补召。"""
        if self._closed:
            return
        if state == c104.ConnectionState.OPEN:
            was_open = self._is_open
            self._is_open = True
            if self._open_event is not None:
                self._open_event.set()
            # c104 自动重连成功且有活动订阅：补发总召刷新镜像（对应旧
            # monitor 的 post-reconnect GI）。首次建连的启动总召由
            # connect() 负责，这里只处理「曾断开后恢复」。
            if not was_open and self._ever_connected and self._subscriptions.has_subscriptions:
                asyncio.ensure_future(self._safe_re_interrogate())
        else:
            self._is_open = False
            # 断线后镜像值不再可信，read 必须回到「尚无数据」语义。
            self._received_ioas.clear()

    async def _safe_re_interrogate(self) -> None:
        """重连后的补召；失败只记日志。"""
        try:
            await self._interrogate()
        except ProtocolError:
            logger.warning(
                "IEC104: post-reconnect general interrogation failed for %s:%d",
                self._config.host,
                self._config.port,
                exc_info=True,
            )

    def _handle_new_point(
        self,
        station: c104.Station,
        io_address: int,
        point_type: c104.Type,
    ) -> None:
        """c104 新点回调（c104 线程，经 callbacks 工厂收敛签名）：
        镜像远端报告的点并挂接收回调。"""
        factory = self._receive_callback_factory
        if self._closed or factory is None:
            return
        point = station.add_point(io_address=io_address, type=point_type)
        if point is None:
            return
        point.on_receive(callable=factory(self._handle_point_receive))

    def _handle_point_receive(self, point: c104.Point) -> c104.ResponseState:
        """c104 监视数据回调（c104 线程，经 callbacks 工厂收敛签名）：
        转换并桥接分发。"""
        ioa = point.io_address
        point_id = self._ioa_to_point_id.get(ioa)
        if point_id is None:
            return c104.ResponseState.NONE
        loop = self._loop
        if loop is None or self._closed:
            return c104.ResponseState.NONE
        pv = point_value_from_c104(point, point_id)
        # loop 已关闭时 call_soon_threadsafe 抛 RuntimeError：停机竞态，丢弃。
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._deliver, pv, ioa)
        return c104.ResponseState.NONE

    def _deliver(self, pv: PointValue, ioa: int) -> None:
        """在事件循环内记录已收数据并异步分发给订阅者。"""
        if self._closed:
            return
        self._received_ioas.add(ioa)
        asyncio.ensure_future(self._subscriptions.dispatch(pv, ioa))


# ---------------------------------------------------------------------------
# 协议自注册
# ---------------------------------------------------------------------------

# 注册必须发生在 Driver 类定义完成后；若注册机制改为组合根显式注入，可移除 E402 抑制。
from wind_hub_core.protocol.registry import register_protocol  # noqa: E402


@register_protocol("iec104")
def _create_iec104(cfg: DeviceConfig) -> ProtocolPort:
    return IEC104Driver(cfg)
