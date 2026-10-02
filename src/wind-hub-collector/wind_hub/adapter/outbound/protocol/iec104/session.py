"""单个 IEC104 从站 TCP 连接的协议 session。

Session 负责 TCP 建连、STARTDT、站总召、APDU 收发、k/w 流控和 t1/t2/t3 timer。
start 后创建独立 receive/send task；close 按 timer → task → socket 顺序释放资源。

Session 维护当前连接的 IOA 最新值缓存，并可把解码后的 ASDU 同步转交 Driver。
它不负责业务 Task 调度、重连策略或跨设备状态；重连由 IEC104Driver 管理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from wind_hub.adapter.outbound.protocol.iec104.codec.apci import (
    IFrame,
    SFrame,
    UFrame,
    decode_apdu,
    encode_i_frame,
    encode_s_frame,
    encode_u_frame,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.asdu import (
    ASDU,
    decode_asdu,
    encode_asdu,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.info_objects import (
    InterrogationCommand,
)
from wind_hub.adapter.outbound.protocol.iec104.codec.types import (
    CauseOfTransmission,
    QualityFlag,
    TypeID,
    UFrameType,
)
from wind_hub.adapter.outbound.protocol.iec104.connection import (
    ConnectionState,
    ConnectionStateMachine,
)
from wind_hub.adapter.outbound.protocol.iec104.flow import FlowController
from wind_hub.adapter.outbound.protocol.iec104.timers import IEC104Timers
from wind_hub.domain.model.errors import ProtocolError
from wind_hub.domain.model.point import PointValue, Quality

logger = logging.getLogger(__name__)

START_CHAR = 0x68

# 会写入 IOA 最新值缓存的监视方向 TypeID。
_MEASUREMENT_TYPE_IDS: frozenset[TypeID] = frozenset(
    {
        TypeID.M_SP_NA_1,
        TypeID.M_SP_TB_1,
        TypeID.M_DP_NA_1,
        TypeID.M_DP_TB_1,
        TypeID.M_ME_NA_1,
        TypeID.M_ME_NB_1,
        TypeID.M_ME_NC_1,
        TypeID.M_ME_TD_1,
        TypeID.M_ME_TF_1,
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


class IEC104Session:
    """单个 IEC104 TCP session。

    Args:
        host: 从站 IP/主机名。
        port: TCP 端口。
        common_addr: 公共地址。
        k: 发送窗口。
        w: 接收确认窗口。
        t1: 发送确认超时秒数。
        t2: 延迟确认超时秒数。
        t3: 空闲保活超时秒数。
    """

    def __init__(
        self,
        host: str,
        port: int,
        common_addr: int,
        k: int = 12,
        w: int = 8,
        t1: float = 15.0,
        t2: float = 10.0,
        t3: float = 20.0,
    ) -> None:
        self._host = host
        self._port = port
        self._common_addr = common_addr
        self._t1 = t1
        self._t2 = t2
        self._t3 = t3

        # 状态机、流控和 timer。
        self._state = ConnectionStateMachine()
        self._flow = FlowController(k=k, w=w)
        self._timers = IEC104Timers(t1=t1, t2=t2, t3=t3)

        # TCP reader/writer 在 start() 成功后设置。
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

        # 出站 APDU 有界队列，防止断网时无限积压。
        self._send_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)

        # TCP 收发后台 task。
        self._receive_task: asyncio.Task[object] | None = None
        self._send_task: asyncio.Task[object] | None = None

        # 站总召完成事件。
        self._interrogation_done: asyncio.Event = asyncio.Event()
        self._interrogation_done.set()  # starts as "done"

        # IOA 到最新 PointValue 的 session 内缓存。
        self._point_cache: dict[int, PointValue] = {}
        # IOA 到 point_id 的映射，由 Driver 注入。
        self._ioa_to_point_id: dict[int, str] = {}
        # point_id 到 IOA 的反向映射。
        self._point_id_to_ioa: dict[str, int] = {}

        # 可选点值更新 callback。
        self._on_value_update: Callable[[PointValue], None] | None = None

        # ASDU 转发 callback，由 Driver 注入。
        self._on_asdu: Callable[[ASDU], None] | None = None

        # STARTDT handshake 同步事件。
        self._startdt_event: asyncio.Event | None = None

        # 关闭标志，阻止后台循环继续工作。
        self._closed = False

    # ==================================================================
    # 状态查询
    # ==================================================================

    @property
    def state(self) -> ConnectionState:
        """返回当前连接状态。"""
        return self._state.state

    @property
    def is_started(self) -> bool:
        """STARTDT 已完成、允许数据传输时为 True。"""
        return self._state.is_started

    @property
    def interrogation_complete(self) -> bool:
        """站总召已结束时为 True。"""
        return self._interrogation_done.is_set()

    @property
    def point_cache(self) -> dict[int, PointValue]:
        """返回 IOA 最新值缓存副本，避免调用方修改内部状态。"""
        return dict(self._point_cache)

    # ==================================================================
    # 点映射与回调
    # ==================================================================

    def set_points_mapping(
        self,
        ioa_to_point_id: dict[int, str],
        point_id_to_ioa: dict[str, int],
    ) -> None:
        """注入 IOA/point_id 双向映射，并复制输入避免外部修改。"""
        self._ioa_to_point_id = dict(ioa_to_point_id)
        self._point_id_to_ioa = dict(point_id_to_ioa)

    def set_on_value_update(
        self,
        callback: Callable[[PointValue], None] | None,
    ) -> None:
        """设置可选的点值更新 callback。"""
        self._on_value_update = callback

    def set_on_asdu(
        self,
        callback: Callable[[ASDU], None] | None,
    ) -> None:
        """设置解码后 ASDU 的同步转发 callback。

        Driver 使用它处理自发数据、总召数据和遥控响应。callback 在 receive loop
        内同步执行，因此不应阻塞。
        """
        self._on_asdu = callback

    def send_asdu(self, asdu: ASDU) -> None:
        """把 ASDU 封装为 I-frame 并加入发送队列。

        Args:
            asdu: 待发送 ASDU。

        Raises:
            ProtocolError: session 尚未完成 STARTDT。

        Side Effects:
            原子占用一个 N(S)，并启动/重启 t1。
        """
        if not self._state.is_started:
            raise ProtocolError(
                f"IEC104: cannot send ASDU — session is not started "
                f"(state={self._state.state.name})"
            )

        send_seq = self._flow.next_send_seq()
        self._enqueue_frame_nowait(
            encode_i_frame(
                send_seq,
                self._flow.recv_seq_for_ack(),
                encode_asdu(asdu),
            )
        )
        self._timers.start_t1()
        logger.debug(
            "IEC104: enqueued ASDU type=%s cause=%s",
            asdu.type_id.name,
            asdu.cause.name,
        )

    def get_ioa(self, point_id: str) -> int | None:
        """按 point_id 查询 IOA；未知点返回 None。"""
        return self._point_id_to_ioa.get(point_id)

    def get_point_id(self, ioa: int) -> str | None:
        """按 IOA 查询 point_id；未知地址返回 None。"""
        return self._ioa_to_point_id.get(ioa)

    # ==================================================================
    # 启动与关闭
    # ==================================================================

    async def start(self) -> None:
        """建立 TCP、完成 STARTDT，并执行启动站总召。

        Raises:
            ProtocolError: session 已关闭、TCP 建连失败或 STARTDT handshake 失败。

        Side Effects:
            创建 receive/send 后台 task，并安装 t1/t2/t3 callback。
        """
        if self._closed:
            raise ProtocolError("Session is closed — create a new one.")

        logger.info(
            "IEC104: connecting to %s:%d (common_addr=%d)",
            self._host,
            self._port,
            self._common_addr,
        )
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self._host,
                self._port,
            )
        except OSError as exc:
            raise ProtocolError(
                f"IEC104: TCP connect to {self._host}:{self._port} failed: {exc}"
            ) from exc

        self._state.to_tcp_connected()
        logger.debug("IEC104: TCP connected to %s:%d", self._host, self._port)

        # TCP 建立后再启动收发 task。
        self._receive_task = asyncio.ensure_future(self._receive_loop())
        self._send_task = asyncio.ensure_future(self._send_loop())

        # timer callback 绑定当前 session。
        self._timers.on_t1_timeout = self._on_t1_timeout
        self._timers.on_t2_timeout = self._on_t2_timeout
        self._timers.on_t3_timeout = self._on_t3_timeout

        # 执行 STARTDT handshake。
        await self._startdt_handshake()

        # STARTDT 后执行站总召。
        await self._station_interrogation()

    async def close(self) -> None:
        """优雅关闭 session。

        若仍 STARTED，尽力发送 STOPDT；随后停止 timer、取消收发 task 并关闭
        socket。重复调用安全，清理阶段的非关键关闭异常被隔离。
        """
        if self._closed:
            return
        self._closed = True

        logger.info("IEC104: closing session to %s:%d", self._host, self._port)

        # STARTED 状态下尽力发送 STOPDT，不因关闭阶段失败阻断清理。
        if self._state.is_started:
            with contextlib.suppress(ValueError):
                self._state.to_stopped()
            with contextlib.suppress(Exception):
                self._enqueue_frame_nowait(encode_u_frame(UFrameType.STOPDT_ACT))

        # 先停止 timer，避免清理期间再触发 callback。
        await self._timers.stop_all()

        # 再取消收发 task。
        for task in (self._receive_task, self._send_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # 最后关闭 TCP writer。
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None

        if self._state.state != ConnectionState.DISCONNECTED:
            self._state.to_disconnected()
        logger.info("IEC104: session closed for %s:%d", self._host, self._port)

    async def wait_closed(self) -> None:
        """等待 receive/send task 退出。

        断线处理会取消 send task（解除 ``queue.get()`` 阻塞）——这种「任务
        被取消」是正常断线路径，用 ``return_exceptions=True`` 收拢；但
        monitor 自身被取消时 gather 抛出的 CancelledError 仍原样向上传播，
        否则停机可能卡在仍运行的后台 task 上。
        """
        tasks = [
            task
            for task in (self._receive_task, self._send_task)
            if task is not None and not task.done()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ==================================================================
    # 出站队列
    # ==================================================================

    def _enqueue_frame_nowait(self, data: bytes) -> None:
        """非阻塞加入出站 APDU；队列满时记录并丢弃该帧。"""
        try:
            self._send_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning(
                "IEC104: send queue full (%d), dropping frame",
                self._send_queue.maxsize,
            )

    async def _enqueue_frame(self, data: bytes) -> None:
        """等待队列空间后加入出站 APDU。"""
        await self._send_queue.put(data)

    # ==================================================================
    # STARTDT handshake
    # ==================================================================

    async def _startdt_handshake(self) -> None:
        """发送 STARTDT_ACT 并等待 STARTDT_CON。

        Raises:
            ProtocolError: t1 时间内未收到确认。
        """
        self._state.to_startdt_pending()
        logger.debug("IEC104: sending STARTDT act to %s", self._host)

        self._startdt_event = asyncio.Event()
        self._enqueue_frame_nowait(encode_u_frame(UFrameType.STARTDT_ACT))

        try:
            await asyncio.wait_for(
                self._startdt_event.wait(),
                timeout=self._t1,
            )
        except TimeoutError:
            raise ProtocolError(f"IEC104: STARTDT handshake timed out after {self._t1}s") from None

        self._startdt_event = None
        self._state.to_started()
        logger.info("IEC104: STARTDT handshake complete for %s", self._host)

    # ==================================================================
    # 站总召
    # ==================================================================

    async def _station_interrogation(self) -> None:
        """发送 C_IC_NA_1（QOI=20）并等待 ACT_TERM。

        总召超时只记录 warning 并结束启动总召等待，不主动关闭已建立的 session。
        """
        self._interrogation_done.clear()

        gi_asdu = ASDU(
            type_id=TypeID.C_IC_NA_1,
            cause=CauseOfTransmission.ACTIVATION,
            common_address=self._common_addr,
            objects=[InterrogationCommand(ioa=0)],  # encoder 固定使用 QOI=20（站总召）。
        )

        logger.info("IEC104: sending station interrogation to %s", self._host)
        send_seq = self._flow.next_send_seq()
        self._enqueue_frame_nowait(
            encode_i_frame(
                send_seq,
                self._flow.recv_seq_for_ack(),
                encode_asdu(gi_asdu),
            )
        )
        self._timers.start_t1()

        # 等待总召 ACT_TERM；给完整总召留出 2*t1。
        try:
            await asyncio.wait_for(
                self._interrogation_done.wait(),
                timeout=self._t1 * 2,  # generous timeout
            )
        except TimeoutError:
            logger.warning(
                "IEC104: station interrogation timed out for %s",
                self._host,
            )
            self._interrogation_done.set()

        logger.info(
            "IEC104: station interrogation complete for %s (%d cached points)",
            self._host,
            len(self._point_cache),
        )

    # ==================================================================
    # 后台收发循环
    # ==================================================================

    async def _receive_loop(self) -> None:
        """持续从 TCP 读取完整 APDU 并分派。

        EOF 会触发断线处理；ProtocolError 记录后继续下一帧，未知 Exception 记录
        但不静默吞掉。任务取消时立即退出。
        """
        buffer = bytearray()
        reader = self._reader
        assert reader is not None

        while not self._closed:
            try:
                # 先读取 start+length 两字节头。
                while len(buffer) < 2:
                    chunk = await reader.read(2 - len(buffer))
                    if not chunk:
                        logger.warning(
                            "IEC104: TCP receive EOF from %s",
                            self._host,
                        )
                        await self._handle_disconnect()
                        return
                    buffer.extend(chunk)

                if buffer[0] != START_CHAR:
                    bad = buffer[0]
                    del buffer[0]
                    logger.warning(
                        "IEC104: unexpected start byte %#04x, skipping",
                        bad,
                    )
                    continue

                apdu_len = buffer[1]
                total_len = 2 + apdu_len

                while len(buffer) < total_len:
                    chunk = await reader.read(total_len - len(buffer))
                    if not chunk:
                        logger.warning(
                            "IEC104: TCP receive EOF mid-frame from %s",
                            self._host,
                        )
                        await self._handle_disconnect()
                        return
                    buffer.extend(chunk)

                frame_bytes = bytes(buffer[:total_len])
                del buffer[:total_len]

                # 收到任何数据都重置 t3 空闲 timer。
                self._timers.cancel_t3()
                self._timers.start_t3()

                frame = decode_apdu(frame_bytes)
                if isinstance(frame, IFrame):
                    await self._handle_i_frame(frame)
                elif isinstance(frame, SFrame):
                    await self._handle_s_frame(frame)
                elif isinstance(frame, UFrame):
                    await self._handle_u_frame(frame)

            except ProtocolError:
                logger.exception(
                    "IEC104: protocol error in receive loop for %s",
                    self._host,
                )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "IEC104: unexpected error in receive loop for %s",
                    self._host,
                )

    async def _send_loop(self) -> None:
        """持续排空发送队列并写入 TCP。

        writer 错误会触发断线处理并退出；CancelledError 用于正常停机。
        """
        writer = self._writer
        assert writer is not None

        while not self._closed:
            try:
                data = await self._send_queue.get()
                writer.write(data)
                await writer.drain()
                self._send_queue.task_done()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "IEC104: error in send loop for %s",
                    self._host,
                )
                await self._handle_disconnect()
                return

    # ==================================================================
    # frame 处理
    # ==================================================================

    async def _handle_i_frame(self, frame: IFrame) -> None:
        """处理入站 I-frame：更新流控、确认状态并解码 ASDU。"""
        self._flow.on_received()

        # 先处理对端 I-frame 携带的 N(R) 确认。
        self._flow.on_ack(frame.recv_seq)
        if not self._flow.ack_is_outstanding():
            self._timers.cancel_t1()

        # 收到 I-frame 后启动/重启 t2 延迟确认。
        self._timers.start_t2()

        # 达到 w 时立即发 S-frame，不再等待 t2。
        if self._flow.needs_ack:
            await self._send_s_ack()

        # 解码并处理本帧携带的 ASDU。
        try:
            asdu, _ = decode_asdu(frame.asdu)
        except ProtocolError:
            logger.exception(
                "IEC104: failed to decode ASDU from %s",
                self._host,
            )
            return

        await self._process_asdu(asdu)

    async def _handle_s_frame(self, frame: SFrame) -> None:
        """处理入站 S-frame 确认，并推进发送侧确认窗口。"""
        self._flow.on_ack(frame.recv_seq)
        if not self._flow.ack_is_outstanding():
            self._timers.cancel_t1()

    async def _handle_u_frame(self, frame: UFrame) -> None:
        """处理入站 U-frame，并完成 STARTDT/STOPDT/TESTFR 控制握手。"""
        if frame.frame_type == UFrameType.STARTDT_CON:
            logger.debug("IEC104: received STARTDT con from %s", self._host)
            if self._startdt_event is not None:
                self._startdt_event.set()

        elif frame.frame_type == UFrameType.STARTDT_ACT:
            logger.debug("IEC104: received STARTDT act from %s", self._host)
            self._enqueue_frame_nowait(encode_u_frame(UFrameType.STARTDT_CON))

        elif frame.frame_type == UFrameType.STOPDT_CON:
            logger.debug("IEC104: received STOPDT con from %s", self._host)

        elif frame.frame_type == UFrameType.STOPDT_ACT:
            logger.debug("IEC104: received STOPDT act from %s", self._host)
            self._enqueue_frame_nowait(encode_u_frame(UFrameType.STOPDT_CON))

        elif frame.frame_type == UFrameType.TESTFR_ACT:
            logger.debug("IEC104: received TESTFR act from %s", self._host)
            self._enqueue_frame_nowait(encode_u_frame(UFrameType.TESTFR_CON))

        elif frame.frame_type == UFrameType.TESTFR_CON:
            logger.debug("IEC104: received TESTFR con from %s", self._host)
            # TESTFR_CON 即 t3 保活的确认——不取消 t1 的话，保活应答到手
            # 后 t1 仍会超时并误杀正常连接。
            self._timers.cancel_t1()

    # ==================================================================
    # ASDU processing
    # ==================================================================

    async def _process_asdu(self, asdu: ASDU) -> None:
        """按 TypeID 与 COT 处理入站 ASDU，并转发给 Driver 回调。"""
        # --- Interrogation lifecycle (session-internal) ---
        if asdu.type_id == TypeID.C_IC_NA_1:
            if asdu.cause == CauseOfTransmission.ACTIVATION_CON:
                logger.debug("IEC104: interrogation activation confirmed")
                # 总召生命周期帧仍需转发给 Driver，供上层完成命令/订阅语义。
                if self._on_asdu is not None:
                    self._on_asdu(asdu)
                return
            if asdu.cause == CauseOfTransmission.ACTIVATION_TERMINATION:
                logger.debug("IEC104: interrogation activation terminated")
                self._interrogation_done.set()
                if self._on_asdu is not None:
                    self._on_asdu(asdu)
                return

        # --- Interrogation data ---
        if asdu.cause == CauseOfTransmission.INTERROGATED_BY_STATION:
            await self._cache_objects(asdu)

        # 其他测量数据（自发、周期等）同样进入点缓存。
        if asdu.type_id in _MEASUREMENT_TYPE_IDS:
            await self._cache_objects(asdu)

        # 最后统一转发给 Driver，由 Driver 完成订阅者分发。
        if self._on_asdu is not None:
            self._on_asdu(asdu)

    async def _cache_objects(self, asdu: ASDU) -> None:
        """解析 ASDU information object，并更新按 IOA 维护的点缓存。"""
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
                timestamp=datetime.now(UTC),
            )
            self._point_cache[ioa] = pv

            if self._on_value_update is not None:
                try:
                    self._on_value_update(pv)
                except Exception:
                    logger.exception(
                        "IEC104: value update callback raised for IOA %d",
                        ioa,
                    )

    # ==================================================================
    # S-frame ack
    # ==================================================================

    async def _send_s_ack(self) -> None:
        """发送 S-frame 确认，并清除当前接收侧待确认计数。"""
        s_frame = encode_s_frame(self._flow.recv_seq_for_ack())
        self._enqueue_frame_nowait(s_frame)
        self._flow.on_ack_sent()
        self._timers.cancel_t2()

    # ==================================================================
    # disconnect handler
    # ==================================================================

    async def _handle_disconnect(self) -> None:
        """处理 TCP 断线事实：切换连接状态、取消全部 IEC104 timer 并解除
        send task 的 ``queue.get()`` 阻塞——否则 :meth:`wait_closed` 永远
        等不到 send task 退出，Driver monitor 无法进入重连。"""
        if self._state.is_connected:
            with contextlib.suppress(ValueError):
                self._state.to_disconnected()

        self._timers.reset_all()

        send_task = self._send_task
        if (
            send_task is not None
            and not send_task.done()
            and send_task is not asyncio.current_task()
        ):
            send_task.cancel()

    # ==================================================================
    # timer callbacks
    # ==================================================================

    async def _on_t1_timeout(self) -> None:
        """处理 t1 超时：对端未在期限内确认，按断线语义处理。

        必须真正关闭 socket：只切状态的话 receive 循环仍阻塞在读上，
        session 既不结束也不重连（半开连接僵尸）。
        """
        logger.warning(
            "IEC104: t1 timeout (%ss) for %s — closing connection",
            self._t1,
            self._host,
        )
        await self._handle_disconnect()
        if self._writer is not None:
            self._writer.close()

    async def _on_t2_timeout(self) -> None:
        """处理 t2 超时：发送延迟的 S-frame 确认。"""
        logger.debug("IEC104: t2 timeout — sending S-frame ack to %s", self._host)
        await self._send_s_ack()

    async def _on_t3_timeout(self) -> None:
        """处理 t3 空闲超时：发送 TESTFR_ACT 并启动 t1 等待确认。"""
        logger.debug("IEC104: t3 timeout — sending TESTFR to %s", self._host)
        self._enqueue_frame_nowait(encode_u_frame(UFrameType.TESTFR_ACT))
        self._timers.start_t1()
