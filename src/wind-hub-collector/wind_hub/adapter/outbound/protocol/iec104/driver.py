"""IEC 60870-5-104 ProtocolPort 实现。

Driver 负责单设备 IEC104 session 生命周期、总召缓存读取、自发数据订阅、遥控
命令及断线重连。具体 APDU/ASDU 编解码、k/w 流控和 t1/t2/t3 timer 由
IEC104Session 及 codec 子模块负责。

read() 读取 session 已维护的最新值缓存，不为每次调用重新发总召；write() 将
领域 Command 转换为遥控 ASDU，并等待 ACT_CON/ACT_TERM 或超时。重连过程中保留
订阅注册，整体 close 才清空订阅。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from wind_hub.adapter.outbound.protocol.iec104.codec.asdu import ASDU
from wind_hub.adapter.outbound.protocol.iec104.codec.info_objects import (
    DoubleCommand,
    InterrogationCommand,
    SetpointCommandShort,
    SingleCommand,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.types import (
    CauseOfTransmission,
    QualityFlag,
    TypeID,
)
from wind_hub.adapter.outbound.protocol.iec104.commands import (
    PendingCommand,
    PendingCommandRegistry,
)
from wind_hub.adapter.outbound.protocol.iec104.config import IEC104Config
from wind_hub.adapter.outbound.protocol.iec104.session import IEC104Session
from wind_hub.adapter.outbound.protocol.iec104.subscriptions import (
    SubscriptionRegistry,
)
from wind_hub.config.schema import DeviceConfig, PointConfig
from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.errors import CommandError, ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue, Quality
from wind_hub.domain.port.outbound import (
    AcquisitionMode,
    HealthStatus,
    ProtocolPort,
    SubscriptionHandle,
)

logger = logging.getLogger(__name__)

# 后台重连的指数退避参数。
_RECONNECT_BACKOFF_BASE = 1.0
_RECONNECT_BACKOFF_CAP = 30.0
_RECONNECT_BACKOFF_MULTIPLIER = 2.0


def _is_timeout_related(exc: BaseException) -> bool:
    """判断会话抛出的协议错误是否源于连接/握手超时。

    ``IEC104Session.start`` 把 TCP 连接失败（``OSError``，errno ETIMEDOUT
    在 Python 3.10+ 映射为 ``TimeoutError``）与 STARTDT 握手超时统一包装为
    ``ProtocolError``；这里经 ``__cause__`` 链与消息文本识别超时类失败，
    供重连循环把这类「对端不可达的日常表现」降级为简洁 warning（决策 2）。
    """
    if isinstance(exc.__cause__, TimeoutError):
        return True
    return "timed out" in str(exc)


# 当前支持的遥控 TypeID。
_CONTROL_TYPE_IDS: frozenset[TypeID] = frozenset(
    {
        TypeID.C_SC_NA_1,
        TypeID.C_DC_NA_1,
        TypeID.C_SE_NC_1,
    }
)


def _extract_value(obj: object) -> object:
    """从 information object 提取测量值；未知结构返回 None。"""
    for attr in ("value", "measured_value", "normalized_value"):
        val = getattr(obj, attr, None)
        if val is not None:
            return val
    return None


def _extract_quality(obj: object) -> Quality:
    """把 IEC104 QualityFlag 映射为 Wind Hub Quality。"""
    q = getattr(obj, "quality", None)
    if q is None or not isinstance(q, QualityFlag):
        return Quality.GOOD
    if QualityFlag.IV in q:
        return Quality.BAD
    if QualityFlag.NT in q or QualityFlag.SB in q:
        return Quality.UNCERTAIN
    if QualityFlag.BL in q or QualityFlag.OV in q:
        return Quality.UNCERTAIN
    return Quality.GOOD


class IEC104Driver:
    """单设备 IEC104 协议驱动。

    Args:
        cfg: 已解析的设备配置。

    Notes:
        Driver 由单个 asyncio event loop 持有。session 自己管理 TCP 收发 task；
        Driver monitor 负责 session 结束后的有限次重连。
    """

    def __init__(self, cfg: DeviceConfig) -> None:
        self._cfg = IEC104Config.from_device_config(cfg)
        self._lock = asyncio.Lock()

        self._session: IEC104Session | None = None
        self._monitor_task: asyncio.Task[object] | None = None
        self._shutdown = False

        # IOA/point_id 双向映射。
        self._ioa_to_point_id: dict[int, str] = {}
        self._point_id_to_ioa: dict[str, int] = {}
        # point_id 到 data_type，用于选择遥控 ASDU 类型。
        self._point_data_types: dict[str, str] = {}

        # 自发数据订阅注册表。
        self._subscriptions = SubscriptionRegistry()

        # 遥控在途命令注册表。
        self._pending_commands = PendingCommandRegistry()

        self._failed = False

    # ==================================================================
    # 点表映射
    # ==================================================================

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """构建设备 IOA/point_id 双向映射。

        Args:
            points: 当前设备点表。

        Notes:
            缺失或非法 IOA 的点会记录 warning 并跳过；重复 IOA 后者覆盖前者。
        """
        ioa_to_point_id: dict[int, str] = {}
        point_id_to_ioa: dict[str, int] = {}

        for p in points:
            try:
                # IEC104 PointAddress 的 ioa 来自协议扩展字段；schema 若改为判别联合类型，应移除此抑制。
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
        # 同时保存数据类型，用于遥控编码决策。
        self._point_data_types = {p.point_id: p.data_type for p in points}
        logger.info(
            "IEC104: mapped %d points for device %s",
            len(ioa_to_point_id),
            self._cfg.host,
        )

    def _resolve_ioa(self, ref: PointRef) -> int | None:
        """把 PointRef 解析为 IOA；未知点返回 None。"""
        return self._point_id_to_ioa.get(ref.point_id)

    # ==================================================================
    # ProtocolPort：连接生命周期
    # ==================================================================

    async def connect(self) -> None:
        """建立 IEC104 session 并启动后台 monitor。

        已有 session 时幂等返回；Driver 已进入 FAILED 时拒绝自动重新连接。

        Raises:
            ProtocolError: STARTDT/TCP 建连失败，或 Driver 已处于 FAILED。
        """
        async with self._lock:
            if self._session is not None:
                logger.warning("IEC104: connect() called but already connected")
                return
            if self._failed:
                raise ProtocolError("IEC104: driver is in FAILED state — manual reset required")

            self._shutdown = False

            session = IEC104Session(
                host=self._cfg.host,
                port=self._cfg.port,
                common_addr=self._cfg.common_addr,
                k=self._cfg.k,
                w=self._cfg.w,
                t1=self._cfg.t1,
                t2=self._cfg.t2,
                t3=self._cfg.t3,
            )
            session.set_points_mapping(
                self._ioa_to_point_id,
                self._point_id_to_ioa,
            )
            # session 解码后的 ASDU 同步转交 Driver。
            session.set_on_asdu(self._on_asdu_received)

            try:
                await session.start()
            except ProtocolError:
                self._failed = True
                raise

            self._session = session
            self._monitor_task = asyncio.ensure_future(self._monitor_loop())

    async def close(self) -> None:
        """关闭 IEC104 session、monitor、在途命令和订阅。

        资源释放顺序很重要：先关闭 session，使其 receive/send task 退出，再取消
        monitor。若先取消 monitor，wait_closed 可能只中断一个 await 而遗留发送 task。
        """
        async with self._lock:
            self._shutdown = True

            # 必须先 close session 再停止 monitor；session close 会取消收发 task，
            # 使 monitor 的 wait_closed 能正常返回并观察 _shutdown。
            if self._session is not None:
                await self._session.close()
                self._session = None

            if self._monitor_task is not None and not self._monitor_task.done():
                self._monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._monitor_task
            self._monitor_task = None

            # 连接关闭后所有在途遥控都必须明确失败完成。
            self._fail_all_pending("connection closed")

            # 注销全部订阅（重连不清——只有整体 close 才清理注册表）。
            self._subscriptions.clear()

            self._failed = False

    def _fail_all_pending(self, reason: str) -> None:
        """把全部在途遥控完成为失败并从 Registry 移除。

        Args:
            reason: 写入 CommandResult.error 的失败原因。
        """
        # remove 会修改注册表，因此先复制 IOA key。
        ioas = list(self._pending_commands._pending.keys())
        for ioa in ioas:
            pending = self._pending_commands.get(ioa)
            if pending is not None and not pending.future.done():
                pending.future.set_result(
                    CommandResult(
                        command_id=pending.command.command_id,
                        success=False,
                        error=reason,
                    )
                )
            self._pending_commands.remove(ioa)

    # ==================================================================
    # ProtocolPort：读取
    # ==================================================================

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """从当前 session 最新值缓存批量读取点。

        Args:
            points: 待读取 PointRef。

        Returns:
            与输入顺序一致的 PointValue；未知/尚无缓存的点返回 Quality.BAD。

        Raises:
            ProtocolError: session 未连接或未 STARTED。
        """
        session = self._session
        if session is None or not session.is_started:
            raise ProtocolError("IEC104: cannot read — driver is not connected")

        cache = session.point_cache
        results: list[PointValue] = []

        for ref in points:
            ioa = session.get_ioa(ref.point_id)
            if ioa is None or ioa not in cache:
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
                pv = cache[ioa]
                results.append(
                    PointValue(
                        device_id=ref.device_id,
                        point_id=ref.point_id,
                        value=pv.value,
                        quality=pv.quality,
                        timestamp=pv.timestamp,
                        source="iec104",
                    )
                )

        return results

    # ==================================================================
    # ProtocolPort：写入
    # ==================================================================

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """并发执行 IEC104 遥控命令。

        Args:
            cmds: 待执行领域 Command。

        Returns:
            与输入顺序一致的 CommandResult；单条命令失败不会取消其他命令。

        Raises:
            ProtocolError: session 未连接或未 STARTED。
        """
        if not cmds:
            return []

        session = self._session
        if session is None or not session.is_started:
            raise ProtocolError("IEC104: cannot write — driver is not connected")

        # 单命令相互独立，可并发等待各自协议确认。
        tasks = [self._execute_one_command(cmd, session) for cmd in cmds]
        return await asyncio.gather(*tasks)

    async def _execute_one_command(
        self,
        cmd: Command,
        session: IEC104Session,
    ) -> CommandResult:
        """执行单条遥控的完整协议生命周期。

        解析 IOA → 构造 ASDU → 注册 PendingCommand → 发送 → 等待确认。协议拒绝、
        IOA 冲突、超时和其他异常都收敛为 CommandResult.success=False，避免单命令
        失败中断批量调用。
        """
        try:
            # 1. point_id → IOA。
            ioa = self._point_id_to_ioa.get(cmd.point_id)
            if ioa is None:
                return CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error=f"unknown point '{cmd.point_id}'",
                )

            # 2. 根据值和点类型构造遥控 ASDU。
            asdu = self._build_control_asdu(cmd, ioa)

            # 3. 注册在途命令，确保同 IOA 不并发。
            future: asyncio.Future[CommandResult] = asyncio.Future()
            timeout = self._cfg.t1 * 2
            pending = PendingCommand(
                command=cmd,
                ioa=ioa,
                future=future,
                timeout=timeout,
            )

            try:
                self._pending_commands.register(pending)
            except CommandError as exc:
                return CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error=str(exc),
                )

            # 4. 发送 ASDU；发送失败立即清理 PendingCommand。
            try:
                session.send_asdu(asdu)
            except ProtocolError:
                self._pending_commands.remove(ioa)
                if not future.done():
                    future.cancel()
                return CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error="session not started",
                )

            # 5. 等待 ACT_CON/ACT_TERM 完成 Future，超时后清理。
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError:
                self._pending_commands.remove(ioa)
                if not future.done():
                    future.cancel()
                return CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error="timeout",
                )

        except Exception as exc:
            return CommandResult(
                command_id=cmd.command_id,
                success=False,
                error=str(exc),
            )

    def _build_control_asdu(self, cmd: Command, ioa: int) -> ASDU:
        """根据 Command.value 与点 data_type 构造遥控 ASDU。

        Args:
            cmd: 原始领域 Command。
            ioa: 已解析目标 IOA。

        Returns:
            C_SC_NA_1、C_DC_NA_1 或 C_SE_NC_1 ASDU。

        Notes:
            Python bool 是 int 子类，因此 bool 判定必须先于 int。数值 1 在单点和
            双点语义间存在歧义，使用点表 data_type 辅助判定。
        """
        val = cmd.value
        data_type = self._point_data_types.get(cmd.point_id, "")

        # bool 或 bool data_type → C_SC_NA_1。
        # bool 是 int 子类，必须优先判断。
        
        if isinstance(val, bool) or data_type == "bool":
            return ASDU(
                type_id=TypeID.C_SC_NA_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=self._cfg.common_addr,
                objects=[SingleCommand(ioa=ioa, value=bool(val), select=False)],
            )

        # float 或 float data_type → C_SE_NC_1。
        if isinstance(val, float) or data_type in ("float32", "float64"):
            return ASDU(
                type_id=TypeID.C_SE_NC_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=self._cfg.common_addr,
                objects=[SetpointCommandShort(ioa=ioa, value=float(val), select=False)],
            )

        # int 根据数值和 data_type 选择单点/双点/设点。
        if isinstance(val, int):
            # value=1 有歧义，使用 data_type 判定：
            # bool/空类型走单点，其余 int/uint 类型走双点。
            if val == 2 or (val == 1 and data_type not in ("", "bool")):
                return ASDU(
                    type_id=TypeID.C_DC_NA_1,
                    cause=CauseOfTransmission.ACTIVATION,
                    common_address=self._cfg.common_addr,
                    objects=[DoubleCommand(ioa=ioa, value=val, select=False)],
                )
            if val in (0, 1):
                return ASDU(
                    type_id=TypeID.C_SC_NA_1,
                    cause=CauseOfTransmission.ACTIVATION,
                    common_address=self._cfg.common_addr,
                    objects=[SingleCommand(ioa=ioa, value=bool(val), select=False)],
                )
            # Large int → set-point.
            return ASDU(
                type_id=TypeID.C_SE_NC_1,
                cause=CauseOfTransmission.ACTIVATION,
                common_address=self._cfg.common_addr,
                objects=[SetpointCommandShort(ioa=ioa, value=float(val), select=False)],
            )

        raise ValueError(f"unsupported value type {type(val).__name__} for control command")

    # ==================================================================
    # ProtocolPort — subscribe
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
        """Register a subscription and return its independent handle.

        数据到达时机由远端决定（spontaneous / periodic / interrogation
        response），``interval`` 对 IEC104 无调度意义，仅透传忽略。
        关闭句柄只注销本次订阅，不关闭设备连接；重连后注册表保留，
        数据流自然恢复。
        """
        return self._subscriptions.subscribe(points, callback, self._resolve_ioa)

    async def interrogate(self) -> None:
        """发送一次 General Interrogation（C_IC_NA_1，QOI=20，master 侧）。

        总召响应经既有 ASDU 接收链进入各订阅回调，不另设返回通道。
        由 ``Device.start_acquisition`` 在订阅建立后触发一次；不做周期
        总召。
        """
        session = self._session
        if session is None or not session.is_started:
            raise ProtocolError("IEC104: cannot interrogate — driver is not connected")
        asdu = ASDU(
            type_id=TypeID.C_IC_NA_1,
            cause=CauseOfTransmission.ACTIVATION,
            common_address=self._cfg.common_addr,
            objects=[InterrogationCommand(ioa=0)],
        )
        session.send_asdu(asdu)
        logger.info("IEC104: general interrogation sent to %s:%d", self._cfg.host, self._cfg.port)

    # ==================================================================
    # ProtocolPort — health
    # ==================================================================

    def health(self) -> HealthStatus:
        """Return current health status."""
        if self._failed:
            return HealthStatus(
                healthy=False,
                message="FAILED — max reconnect retries exhausted",
            )
        session = self._session
        if session is None:
            return HealthStatus(healthy=False, message="not connected")
        if session.is_started:
            return HealthStatus(
                healthy=True,
                message=f"connected to {self._cfg.host}:{self._cfg.port}",
            )
        return HealthStatus(
            healthy=False,
            message=f"state={session.state.name}",
        )

    # ==================================================================
    # Session 回调的 ASDU 分发
    # ==================================================================

    def _on_asdu_received(self, asdu: ASDU) -> None:
        """按 TypeID/COT 分发 session 收到的 ASDU。

        callback 在 session receive loop 中同步执行，因此这里只做轻量状态更新；
        subscriber 分发会另起 task。异常被记录并隔离，避免 callback 破坏接收循环。
        """
        try:
            # 遥控响应。
            if asdu.type_id in _CONTROL_TYPE_IDS:
                if asdu.cause == CauseOfTransmission.ACTIVATION_CON:
                    self._on_activation_con(asdu)
                elif asdu.cause == CauseOfTransmission.ACTIVATION_TERMINATION:
                    self._on_activation_term(asdu)
                return

            # 监视数据 → subscriber。
            if asdu.cause in (
                CauseOfTransmission.SPONTANEOUS,
                CauseOfTransmission.INTERROGATED_BY_STATION,
                CauseOfTransmission.PERIODIC,
                CauseOfTransmission.BACKGROUND,
                CauseOfTransmission.INITIALIZED,
                CauseOfTransmission.REQUEST,
            ):
                self._dispatch_point_values(asdu)

        except Exception:
            logger.exception("IEC104: error in _on_asdu_received")

    def _dispatch_point_values(self, asdu: ASDU) -> None:
        """把监视方向 information object 转为 PointValue，并异步分发给订阅者。"""
        for obj in asdu.objects:
            ioa: int = getattr(obj, "ioa", 0)
            point_id = self._ioa_to_point_id.get(ioa)
            if point_id is None:
                continue

            value = _extract_value(obj)
            quality = _extract_quality(obj)
            pv = PointValue(
                device_id="",
                point_id=point_id,
                value=value,
                quality=quality,
                source="iec104",
            )

            # 通过 SubscriptionRegistry 异步分发，避免阻塞 receive loop。
            asyncio.ensure_future(self._subscriptions.dispatch(pv, ioa))

    def _on_activation_con(self, asdu: ASDU) -> None:
        """处理遥控 ACT_CON。

        当前 ASDU 模型只保留 COT 低 6 bit，未保留 P/N 原始位，因此这里无法可靠
        判定 negative confirmation，暂按正向确认处理；这是明确的协议模型限制。
        """
        # codec 当前未保存 COT 的 P/N 位，无法在这里可靠区分正/负确认；
        # 在 wire model 增加原始 COT 标志前，暂按 positive 处理。
        for obj in asdu.objects:
            ioa: int = getattr(obj, "ioa", 0)
            # 当前模型缺少 P/N 位，只能按 positive confirmation 处理。
            self._pending_commands.on_activation_con(ioa, negative=False)

    def _on_activation_term(self, asdu: ASDU) -> None:
        """处理遥控 ACT_TERM，并完成匹配 PendingCommand。"""
        for obj in asdu.objects:
            ioa: int = getattr(obj, "ioa", 0)
            self._pending_commands.on_activation_term(ioa)

    # ==================================================================
    # 重连与 monitor
    # ==================================================================

    async def _monitor_loop(self) -> None:
        """监视 session 结束并按指数退避重连。

        连接丢失会先失败完成所有在途命令，但保留 SubscriptionRegistry；重连成功后
        新 session 重新绑定点映射与 ASDU callback。超过重试上限进入 FAILED。
        """
        retries = 0
        backoff = _RECONNECT_BACKOFF_BASE

        while not self._shutdown:
            session = self._session
            if session is not None:
                await session.wait_closed()
                if self._shutdown:
                    return

                logger.warning(
                    "IEC104: session to %s:%d closed — reconnecting",
                    self._cfg.host,
                    self._cfg.port,
                )
                self._session = None
                self._fail_all_pending("connection lost")

            retries += 1
            if retries > self._cfg.max_reconnect_retries:
                logger.error(
                    "IEC104: max reconnect retries (%d) exhausted for %s:%d",
                    self._cfg.max_reconnect_retries,
                    self._cfg.host,
                    self._cfg.port,
                )
                self._failed = True
                return

            await asyncio.sleep(backoff)
            backoff = min(
                backoff * _RECONNECT_BACKOFF_MULTIPLIER,
                _RECONNECT_BACKOFF_CAP,
            )

            if self._shutdown:
                return

            logger.info(
                "IEC104: reconnect attempt %d/%d to %s:%d (backoff=%.1fs)",
                retries,
                self._cfg.max_reconnect_retries,
                self._cfg.host,
                self._cfg.port,
                backoff,
            )

            try:
                new_session = IEC104Session(
                    host=self._cfg.host,
                    port=self._cfg.port,
                    common_addr=self._cfg.common_addr,
                    k=self._cfg.k,
                    w=self._cfg.w,
                    t1=self._cfg.t1,
                    t2=self._cfg.t2,
                    t3=self._cfg.t3,
                )
                new_session.set_points_mapping(
                    self._ioa_to_point_id,
                    self._point_id_to_ioa,
                )
                new_session.set_on_asdu(self._on_asdu_received)
                await new_session.start()
            except TimeoutError:
                # 连接/握手超时是对端不可达的日常表现：降级为简洁 warning，
                # 不打堆栈（决策 2）；其他协议错误仍保留完整堆栈便于排查。
                logger.warning(
                    "IEC104: reconnect timed out for %s:%d",
                    self._cfg.host,
                    self._cfg.port,
                )
                continue
            except ProtocolError as exc:
                if _is_timeout_related(exc):
                    logger.warning(
                        "IEC104: reconnect timed out for %s:%d",
                        self._cfg.host,
                        self._cfg.port,
                    )
                else:
                    logger.exception(
                        "IEC104: reconnect failed for %s:%d",
                        self._cfg.host,
                        self._cfg.port,
                    )
                continue

            self._session = new_session
            retries = 0
            backoff = _RECONNECT_BACKOFF_BASE
            logger.info(
                "IEC104: reconnected to %s:%d",
                self._cfg.host,
                self._cfg.port,
            )


# ---------------------------------------------------------------------------
# 协议自注册
# ---------------------------------------------------------------------------

# 注册必须发生在 Driver 类定义完成后；若注册机制改为组合根显式注入，可移除 E402 抑制。
from wind_hub.infra.protocol_registry import register_protocol  # noqa: E402


@register_protocol("iec104")
def _create_iec104(cfg: DeviceConfig) -> ProtocolPort:
    return IEC104Driver(cfg)
