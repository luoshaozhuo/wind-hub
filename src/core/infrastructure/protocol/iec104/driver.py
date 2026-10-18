"""基于 hat.drivers.iec104（纯 asyncio IEC 60870-5-104）的 ProtocolPort Adapter。

能力边界（只暴露真实协议能力）：

- 支持单点主动读取（C_RD_NA_1）、单点遥控/设点、按 CA 总召、被动监听；
- 不支持任意 IOA 集合批量读、不支持原子批量写、不支持「订阅」——IEC104
  没有远程订阅原语，主动上送只能本地 ``listen``。

架构要点：

- 一条 TCP 连接（一个 hat APCI 会话）承载多个 Common Address，完整
  协议地址 ``(ca, ioa)`` 键控全部索引与请求关联；
- 无跨线程回调、无本地 Point 注册：hat 直接解码 ASDU，请求-响应在
  单一 asyncio 事件循环内用 Future 关联；
- 每次 ``connect`` 产生全新的会话对象，旧会话终止时只能结束自己的
  Future，结构上不存在旧回调污染新会话；
- Driver 不自动重连、不做第二只看门狗：链路终止后由上层 Watchdog
  重新 connect/restore，Driver 只提供真实的健康证据。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from hat.drivers import net
from hat.drivers.iec101 import common as iec101
from hat.drivers.iec104 import common as iec104common
from hat.drivers.iec104 import encoder as iec104_encoder
from hat.drivers.iec60870 import apci

from core.application.errors import (
    ConfigError,
    ProtocolCapabilityError,
    ProtocolConnectionError,
    ProtocolError,
)
from core.application.port.protocol import ListenHandle, ProtocolSampleCallback, SubscriptionHandle
from core.application.protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)
from core.domain import ConnectionEndpoint, PointTable, ProtocolOptions

from .codec import (
    build_command,
    decode_sample,
    negative_read_message,
)
from .config import IEC104Config, parse_iec104_config
from .listener import ListenerRegistry, ProtocolListenerCallback
from .mapping import (
    IEC104Point,
    build_iec104_index,
    validate_iec104_write_type,
)

logger = logging.getLogger(__name__)

Address = tuple[int, int]
_PROBE_UNSUPPORTED_CAUSES = frozenset(
    {
        iec101.ReadResCause.UNKNOWN_TYPE,
        iec101.ReadResCause.UNKNOWN_CAUSE,
        iec101.ReadResCause.UNKNOWN_ASDU_ADDRESS,
        iec101.ReadResCause.UNKNOWN_IO_ADDRESS,
    }
)


class IEC104InterrogationIncompleteError(ProtocolError):
    """总召未得到终结证据；samples 为终止前收到的部分有效结果。"""

    def __init__(self, ca: int, qoi: int, samples: tuple[ProtocolSample, ...], reason: str):
        super().__init__(
            f"IEC104 interrogation incomplete for ca={ca}, qoi={qoi}: {reason}; "
            f"partial_samples={len(samples)}"
        )
        self.common_address = ca
        self.qoi = qoi
        self.samples = samples
        self.completed = False


class IEC104ActiveReadRejectedError(ProtocolError):
    """从站对 C_RD_NA_1 给出否定响应（应用层拒绝，非链路故障）。"""

    def __init__(self, cause: object) -> None:
        super().__init__(negative_read_message(cause))
        self.cause = cause


def _set_result(future: asyncio.Future[Any], value: Any) -> None:
    """完成 Future 的幂等辅助（重复报文/会话结束竞态下不抛 InvalidStateError）。"""
    if not future.done():
        future.set_result(value)


def _set_exception(future: asyncio.Future[Any], exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)


@dataclass(slots=True)
class _ReadRequest:
    future: asyncio.Future[ProtocolSample]
    point_id: str


@dataclass(slots=True)
class _Session:
    """一次 TCP/STARTDT 会话的全部可变状态；断连即整体丢弃。"""

    conn: Any
    task: asyncio.Task[None] | None
    enabled: bool = True
    failed: bool = False
    next_originator: int = 0
    reads: dict[Address, _ReadRequest] = field(default_factory=dict)
    commands: dict[Address, asyncio.Future[bool]] = field(default_factory=dict)
    command_accepted: set[Address] = field(default_factory=set)
    interrogations: dict[int, asyncio.Future[None]] = field(default_factory=dict)
    interrogation_samples: dict[int, list[ProtocolSample]] = field(default_factory=dict)
    interrogation_qoi: dict[int, int] = field(default_factory=dict)
    interrogation_accepted: set[int] = field(default_factory=set)

    def originator(self) -> int:
        """分配 1..255 的始发地址（诊断用；响应关联以完整地址+串行锁为准）。"""
        self.next_originator = self.next_originator % 255 + 1
        return self.next_originator


class _KeyLocks:
    """按完整地址引用计数的串行锁集合（计数与字典维护在同一同步段，零泄漏）。"""

    def __init__(self) -> None:
        self._locks: dict[object, asyncio.Lock] = {}
        self._refs: dict[object, int] = {}

    @contextlib.asynccontextmanager
    async def locked(self, key: object) -> AsyncIterator[None]:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
            self._refs[key] = 0
        self._refs[key] += 1
        try:
            async with lock:
                yield
        finally:
            self._refs[key] -= 1
            if self._refs[key] == 0:
                self._locks.pop(key, None)
                self._refs.pop(key, None)


class _ListenHandleImpl(ListenHandle):
    """``listen`` 返回的句柄；重复 close 安全。"""

    def __init__(self, owner: IEC104Driver, listener_id: int) -> None:
        self._owner = owner
        self._id = listener_id
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._owner._remove_listener(self._id)


class IEC104Driver:
    """单个 Endpoint、多 CA 的 IEC104 主站 Driver。

    Driver 不缓存业务点值；重连退避与业务恢复由上层负责。
    """

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ) -> None:
        if point_table.protocol.name != "iec104":
            raise ConfigError(
                f"point table '{point_table.point_table_id}' protocol is "
                f"'{point_table.protocol.name}', expected 'iec104'"
            )

        self._point_table_id = point_table.point_table_id
        self._config: IEC104Config = parse_iec104_config(endpoint, protocol_options)
        self._points_by_id: dict[str, IEC104Point]
        self._points_by_address: dict[Address, IEC104Point]
        self._points_by_id, self._points_by_address = self._build_index(point_table)
        self._validate_health_point()

        self._lifecycle = asyncio.Lock()
        self._session: _Session | None = None
        self._read_locks = _KeyLocks()
        self._command_locks = _KeyLocks()
        self._gi_locks = _KeyLocks()
        self._listeners = ListenerRegistry()
        # close 时存在未收敛回调、未能一次性关闭成功的旧注册表：继续持有，
        # 后续 close() 重新等待其任务结束，不丢失对遗留任务的追踪。
        self._closing_listeners: list[ListenerRegistry] = []
        self._encoder = iec104_encoder.Encoder()

    # ------------------------------------------------------------------
    # 契约
    # ------------------------------------------------------------------

    def capabilities(self) -> frozenset[ProtocolCapability]:
        """IEC104 真实能力：单点读、单点写、总召、被动监听。"""
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
                ProtocolCapability.INTERROGATE,
                ProtocolCapability.LISTEN,
            }
        )

    def update_point_table(self, point_table: PointTable) -> None:
        """热重载点表：重建 ``(ca, ioa)`` 寻址索引（不断开连接）。

        采用新表前拒绝全部在途请求，避免旧表语义的响应污染新状态。
        """
        next_by_id, next_by_address = self._build_index(point_table)
        self._point_table_id = point_table.point_table_id
        self._points_by_id = next_by_id
        self._points_by_address = next_by_address
        session = self._session
        if session is not None:
            # 只拒绝旧表在途请求；会话本身仍然有效，不标记 failed。
            self._reject_pending(
                session, ProtocolError("IEC104 point table changed; request aborted")
            )
        self._validate_health_point()

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """建立一条 TCP + STARTDT 会话；已连接时幂等返回。"""
        async with self._lifecycle:
            if self._session is not None:
                return
            try:
                raw_conn = await asyncio.wait_for(self._dial(), timeout=self._config.t0)
            except TimeoutError as exc:
                raise ProtocolError(
                    f"IEC104 connect to {self._config.host}:{self._config.port} "
                    f"timed out after {self._config.t0}s"
                ) from exc
            except (ProtocolError, RuntimeError):
                raise
            except Exception as exc:
                raise ProtocolError(
                    f"IEC104 connect failed for {self._config.host}:"
                    f"{self._config.port}: {exc}"
                ) from exc

            session = _Session(conn=raw_conn, task=None)
            raw_conn.register_enabled_cb(
                lambda enabled, s=session: self._on_enabled(s, enabled)
            )
            session.enabled = bool(raw_conn.is_enabled)
            session.task = asyncio.create_task(
                self._receive_loop(session), name="iec104-receive"
            )
            self._session = session
            logger.info(
                "IEC104 connected to %s:%s, CAs=%s",
                self._config.host,
                self._config.port,
                list(self._config.common_addresses),
            )

    async def _dial(self) -> Any:
        """建立 hat APCI 连接（含 STARTDT 握手等待）。测试 seam。"""
        return await apci.connect(
            addr=net.TcpAddress(self._config.host, self._config.port),
            response_timeout=self._config.t1,
            supervisory_timeout=self._config.t2,
            test_timeout=self._config.t3,
            send_window_size=self._config.k,
            receive_window_size=self._config.w,
        )

    async def close(self) -> None:
        """关闭当前会话与监听分发表；幂等，可随后再次 connect。

        会话释放先于监听收敛且其失败不阻断连接关闭；监听回调在取消后仍
        未结束时，旧注册表被保留到 ``_closing_listeners``，本方法抛出
        ``TimeoutError``，再次调用 ``close()`` 会继续等待遗留任务收敛。
        全流程持生命周期锁，并发关闭串行执行。
        """
        async with self._lifecycle:
            session = self._session
            self._session = None
            if session is not None:
                self._fail_session(
                    session,
                    ProtocolConnectionError("IEC104 connection closed"),
                )
                if session.task is not None:
                    session.task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await session.task
                with contextlib.suppress(Exception):
                    await session.conn.async_close()
            # 全部监听随会话结束；重连后由上层 restore 重新注册。换新注册表。
            # 上次 close 超时的旧注册表仍在 _closing_listeners 中，一并重新
            # 等待；仍有未结束回调时保留引用并在最后显式抛出 TimeoutError。
            old_registry, self._listeners = self._listeners, ListenerRegistry()
            registries = [*self._closing_listeners, old_registry]
            pending: list[ListenerRegistry] = []
            for registry in registries:
                try:
                    await registry.close()
                except TimeoutError:
                    pending.append(registry)
            self._closing_listeners = pending
            if pending:
                raise TimeoutError(
                    f"IEC104 close: {len(pending)} listener registry(ies) still "
                    f"have callback task(s) running after cancellation"
                )

    def is_open(self) -> bool:
        """STARTDT 数据传输已使能且会话存在（纯本地查询）。"""
        session = self._session
        return session is not None and session.enabled

    # ------------------------------------------------------------------
    # 健康
    # ------------------------------------------------------------------

    async def health(self) -> ConnectionHealth:
        """链路健康（APCI/STARTDT）+ 可选应用层探测（真实单点读取证据）。

        未配置 ``health_point`` 时只报告链路状态；配置后主动读一次该点，
        必须拿到本次响应才算应用层健康。从站以 UNKNOWN_* 否定 C_RD 时，
        明确表达「不支持应用层探测」，不误报链路故障。
        """
        session = self._session
        if session is None:
            return ConnectionHealth(healthy=False, message="not connected")
        if not session.enabled:
            return ConnectionHealth(
                healthy=False, message="IEC104 data transfer stopped (STARTDT disabled)"
            )
        link_message = (
            f"IEC104 link OPEN {self._config.host}:{self._config.port}"
            f" (CAs={list(self._config.common_addresses)}, t3={self._config.t3}s)"
        )
        health_point = self._config.health_point
        if health_point is None:
            # 只有链路/APCI 证据，文案必须显式说明未做应用层探测。
            return ConnectionHealth(
                healthy=True,
                message=f"{link_message}; link/APCI evidence only, no application probe",
            )

        mapped = self._points_by_id.get(health_point)
        if mapped is None:
            return ConnectionHealth(
                healthy=False,
                message=f"health point '{health_point}' missing from point table",
            )
        probe_location = f"ca={mapped.common_address}, ioa={mapped.ioa}"
        try:
            await self.read_one(health_point)
        except IEC104ActiveReadRejectedError as exc:
            if exc.cause in _PROBE_UNSUPPORTED_CAUSES:
                # 显式 C_RD 拒绝（UNKNOWN_*）：链路仍然健康，只是该点
                # 不支持主动读探测；不断开连接、不触发任何恢复动作。
                return ConnectionHealth(
                    healthy=True,
                    message=(
                        f"{link_message}; application probe unsupported at {probe_location}"
                        f" (station rejected C_RD: {exc.cause}); link stays OPEN"
                    ),
                )
            return ConnectionHealth(
                healthy=False,
                message=f"application probe rejected at {probe_location}: {exc}",
            )
        except ProtocolConnectionError as exc:
            return ConnectionHealth(
                healthy=False,
                message=f"link failed during application probe at {probe_location}: {exc}",
            )
        except ProtocolError as exc:
            # 读取超时/解码失败等：区分超时与其他读取失败，底层原因保留。
            kind = "timed out" if "timed out" in str(exc) else "failed"
            return ConnectionHealth(
                healthy=False,
                message=f"application probe {kind} at {probe_location}: {exc}",
            )
        # 拿到本次新鲜响应即应用层健康——即使样本品质位 BAD（坏品质是
        # 合法的通信证据，不代表链路不健康）。
        return ConnectionHealth(
            healthy=True,
            message=f"{link_message}; application probe ok at {probe_location}",
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def read_one(self, point_id: str) -> ProtocolSample:
        """发送 C_RD_NA_1 并等待本次请求的响应（按 (ca,ioa) 精确关联）。

        不使用任何缓存；自发/总召数据不能完成本 Future。同地址并发读取
        串行化；超时、否定响应、断线都显式失败。
        """
        mapped = self._require_point(point_id)
        if mapped.type_id is not None and not mapped.type_id.startswith("M_"):
            raise ProtocolError(
                f"IEC104 point '{point_id}' is a command point ({mapped.type_id}), "
                "active read requires a monitoring (M_*) point"
            )
        session = self._require_session()
        key = (mapped.common_address, mapped.ioa)
        async with self._read_locks.locked(key):
            if self._session is not session:
                raise ProtocolConnectionError("IEC104 session changed while waiting to read")
            future: asyncio.Future[ProtocolSample] = asyncio.get_running_loop().create_future()
            request = _ReadRequest(future=future, point_id=mapped.point_id)
            session.reads[key] = request
            try:
                await self._send(
                    session,
                    [
                        iec101.ReadMsg(
                            is_test=False,
                            originator_address=session.originator(),
                            asdu_address=mapped.common_address,
                            io_address=mapped.ioa,
                            cause=iec101.ReadReqCause.REQUEST,
                        )
                    ],
                )
                try:
                    return await asyncio.wait_for(future, timeout=self._config.t1)
                except TimeoutError as exc:
                    self._invalidate_session(session, "active read timed out")
                    raise ProtocolError(
                        f"IEC104 active read timed out for '{point_id}'"
                    ) from exc
            except asyncio.CancelledError:
                # 请求可能已发送；隔离迟到响应，禁止复用旧会话。
                self._invalidate_session(session, "active read cancelled")
                raise
            finally:
                if session.reads.get(key) is request:
                    session.reads.pop(key, None)

    async def read_many(
        self,
        point_ids: Sequence[str],
    ) -> tuple[ProtocolSample, ...]:
        """IEC104 无任意 IOA 集合的原生批量读取，禁止循环单点伪装。"""
        del point_ids
        raise ProtocolCapabilityError("iec104 does not support native batch read_many")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        """执行单点遥控/设点，严格按应用层确认证据三分类。

        - 本地校验失败 / 从站否定确认（含 UNKNOWN_* COT）→ CONFIRMED_FAILURE；
        - 肯定 ACT_CON 后肯定 ACT_TERM → CONFIRMED_SUCCESS（协议流程完成）；
        - 超时、断线等丢失确认 → UNKNOWN（可能已送达，禁止自动重传）。
        """
        mapped = self._require_point(write.point)
        try:
            validate_iec104_write_type(mapped)
            command = build_command(mapped, write.value)
        except (ConfigError, ProtocolError, TypeError, ValueError) as exc:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                outcome=WriteOutcome.CONFIRMED_FAILURE,
                message=str(exc),
            )

        session = self._require_session()
        key = (mapped.common_address, mapped.ioa)
        async with self._command_locks.locked(key):
            if self._session is not session:
                return ProtocolWriteResult(
                    point_id=mapped.point_id,
                    outcome=WriteOutcome.UNKNOWN,
                    message="IEC104 session changed during command",
                )
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            session.commands[key] = future
            session.command_accepted.discard(key)
            try:
                try:
                    await self._send(
                        session,
                        [
                            # 注意：iec104 的 CommandMsg 是带 time 的独立
                            # NamedTuple（其余 Msg 才是 iec101 类型别名）。
                            iec104common.CommandMsg(
                                is_test=False,
                                originator_address=session.originator(),
                                asdu_address=mapped.common_address,
                                io_address=mapped.ioa,
                                command=command,
                                is_negative_confirm=False,
                                time=None,
                                cause=iec101.CommandReqCause.ACTIVATION,
                            )
                        ],
                    )
                except ProtocolConnectionError as exc:
                    return ProtocolWriteResult(
                        point_id=mapped.point_id,
                        outcome=WriteOutcome.UNKNOWN,
                        message=f"command delivery failed: {exc}",
                    )
                try:
                    accepted = await asyncio.wait_for(future, timeout=self._config.t1 * 2)
                except TimeoutError:
                    self._invalidate_session(session, "command confirmation timed out")
                    return ProtocolWriteResult(
                        point_id=mapped.point_id,
                        outcome=WriteOutcome.UNKNOWN,
                        message=(
                            "command termination timed out — command may have executed"
                        ),
                    )
                except ProtocolConnectionError as exc:
                    return ProtocolWriteResult(
                        point_id=mapped.point_id,
                        outcome=WriteOutcome.UNKNOWN,
                        message=f"connection lost after command send: {exc}",
                    )
            except asyncio.CancelledError:
                # 请求可能已上线；取消后不能让迟到确认为下一条同地址控制背书。
                self._invalidate_session(session, "command cancelled with uncertain delivery")
                raise
            finally:
                session.commands.pop(key, None)
                session.command_accepted.discard(key)
            if accepted:
                return ProtocolWriteResult(
                    point_id=mapped.point_id,
                    outcome=WriteOutcome.CONFIRMED_SUCCESS,
                )
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                outcome=WriteOutcome.CONFIRMED_FAILURE,
                message="station returned negative command confirmation",
            )

    async def write_many(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """IEC104 无原子批量遥控，明确拒绝。"""
        del writes
        raise ProtocolCapabilityError("iec104 does not support atomic batch write_many")

    # ------------------------------------------------------------------
    # 总召
    # ------------------------------------------------------------------

    async def interrogate(
        self, common_address: int | None = None, *, qoi: int = 20
    ) -> tuple[ProtocolSample, ...]:
        """对指定 CA（缺省：全部已配置 CA 逐个）发送 C_IC_NA_1（QOI=20 站总召，21..36 组召）。

        以每个 CA 和当前 QOI 对应的 ACT_TERM（或否定确认）为完成依据；请求发送成功
        不代表总召完成。ACT_TERM 只表示协议总召流程结束，业务测点完整性
        由 Collector 判断。
        """
        if not isinstance(qoi, int) or isinstance(qoi, bool) or not 20 <= qoi <= 36:
            raise ConfigError("IEC104 interrogation QOI must be an integer in 20..36")
        if common_address is None:
            batches: list[ProtocolSample] = []
            for ca in self._config.common_addresses:
                try:
                    batches.extend(await self._interrogate_one(ca, qoi=qoi))
                except IEC104InterrogationIncompleteError as exc:
                    raise IEC104InterrogationIncompleteError(
                        ca, qoi, (*batches, *exc.samples), str(exc)
                    ) from exc
            return tuple(batches)
        if common_address not in self._config.common_addresses:
            raise ConfigError(
                f"IEC104 common address {common_address} is not configured "
                f"(configured: {list(self._config.common_addresses)})"
            )
        return await self._interrogate_one(common_address, qoi=qoi)

    async def _interrogate_one(self, ca: int, *, qoi: int) -> tuple[ProtocolSample, ...]:
        session = self._require_session()
        async with self._gi_locks.locked(ca):
            if self._session is not session:
                raise ProtocolConnectionError("IEC104 session changed during interrogation")
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            session.interrogations[ca] = future
            session.interrogation_qoi[ca] = qoi
            session.interrogation_accepted.discard(ca)
            session.interrogation_samples[ca] = []
            try:
                await self._send(
                    session,
                    [
                        iec101.InterrogationMsg(
                            is_test=False,
                            originator_address=session.originator(),
                            asdu_address=ca,
                            request=qoi,
                            is_negative_confirm=False,
                            cause=iec101.CommandReqCause.ACTIVATION,
                        )
                    ],
                )
                try:
                    await asyncio.wait_for(future, timeout=self._config.t1 * 2)
                    return tuple(session.interrogation_samples[ca])
                except TimeoutError as exc:
                    partial = tuple(session.interrogation_samples[ca])
                    self._invalidate_session(session, "interrogation timed out")
                    raise IEC104InterrogationIncompleteError(
                        ca, qoi, partial, "ACT_TERM timed out"
                    ) from exc
                except ProtocolConnectionError as exc:
                    raise IEC104InterrogationIncompleteError(
                        ca, qoi, tuple(session.interrogation_samples[ca]), str(exc)
                    ) from exc
            except asyncio.CancelledError:
                # 总召取消后可能仍有分批响应在途，隔离旧会话。
                self._invalidate_session(session, "interrogation cancelled")
                raise
            finally:
                session.interrogations.pop(ca, None)
                session.interrogation_qoi.pop(ca, None)
                session.interrogation_accepted.discard(ca)
                session.interrogation_samples.pop(ca, None)

    # ------------------------------------------------------------------
    # 推送：只有本地监听，没有远程订阅
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        """IEC104 无远程订阅原语；主动上送使用 listen。"""
        del point_ids, callback, interval
        raise ProtocolCapabilityError(
            "iec104 does not support remote subscription — use listen for spontaneous data"
        )

    async def listen(
        self,
        point_ids: Sequence[str],
        callback: ProtocolListenerCallback,
        *,
        interval: float | None = None,
    ) -> ListenHandle:
        """注册本地监听（按配置 point_id 过滤）；不向从站注册、不触发总召。

        空 ``point_ids`` 监听全部已配置点。未配置 (ca,ioa) 的上送不投递，
        只记 debug 日志，避免污染业务点表。
        """
        del interval
        if self._session is None:
            raise ProtocolError("IEC104 listen requires an established connection")
        if point_ids:
            for point_id in point_ids:
                self._require_point(point_id)
            filter_ids = tuple(dict.fromkeys(point_ids))
        else:
            filter_ids = tuple(self._points_by_id)
        listener_id = self._listeners.add(filter_ids, callback)
        return _ListenHandleImpl(self, listener_id)

    async def _remove_listener(self, listener_id: int) -> None:
        await self._listeners.remove(listener_id)

    # ------------------------------------------------------------------
    # 接收管线
    # ------------------------------------------------------------------

    async def _receive_loop(self, session: _Session) -> None:
        """单接收任务：APCI APDU → ASDU 解码 → 会话内分发。

        未知 ASDU/IO 类型只记录日志并跳过，不允许杀死接收管线。
        """
        try:
            while True:
                data = await session.conn.receive()
                received_at = datetime.now(UTC)
                self._decode_and_dispatch(session, data, receive_time=received_at)
        except asyncio.CancelledError:
            raise
        except ConnectionError:
            pass
        except Exception:
            logger.exception("IEC104 receive loop terminated unexpectedly")
        finally:
            await self._on_session_end(session)

    def _decode_and_dispatch(
        self, session: _Session, data: bytes, *, receive_time: datetime | None = None
    ) -> None:
        # hat Encoder.decode 是生成器（单 ASDU 多 IO 元素展开为多条 Msg），
        # 异常可能在任何一次 next() 抛出——本协议栈在进入生成器时急切解码
        # 整个 ASDU，截断/非法类型通常在第一次 next() 即失败。解码与逐条
        # 分发严格隔离：当前 APDU 不可解码的剩余部分只丢这一帧并记日志，
        # 绝不杀死接收任务，后续 APDU 正常处理。出错后不重新迭代同一生成器。
        messages = self._encoder.decode(data)
        batch: list[ProtocolSample] = []
        while True:
            try:
                message = next(messages)
            except StopIteration:
                break
            except iec101.AsduTypeError as exc:
                logger.warning(
                    "IEC104 unsupported ASDU type (%s); APDU remainder dropped: %s",
                    exc,
                    data.hex(),
                )
                break
            except Exception:
                logger.exception(
                    "IEC104 ASDU decode failed (malformed/truncated APDU); "
                    "remainder of this APDU dropped: %s",
                    data.hex(),
                )
                break
            try:
                self._dispatch(session, message, receive_time=receive_time, batch=batch)
            except Exception:
                logger.exception(
                    "IEC104 dispatch failed for %s ca=%s ioa=%s; "
                    "subsequent messages are unaffected",
                    type(message).__name__,
                    getattr(message, "asdu_address", "?"),
                    getattr(message, "io_address", "?"),
                )
        if batch:
            self._listeners.offer(tuple(batch))

    def _dispatch(
        self, session: _Session, message: iec101.Msg, *,
        receive_time: datetime | None = None,
        batch: list[ProtocolSample] | None = None,
    ) -> None:
        if isinstance(message, iec101.DataMsg):
            self._dispatch_data(session, message, receive_time=receive_time, batch=batch)
        elif isinstance(message, iec101.ReadMsg):
            self._dispatch_read_negative(session, message)
        elif isinstance(message, iec104common.CommandMsg):
            self._dispatch_command(session, message)
        elif isinstance(message, iec101.InterrogationMsg):
            self._dispatch_interrogation(session, message)
        # 时钟同步、初始化、参数等报文当前无业务语义，忽略。

    def _dispatch_data(
        self, session: _Session, message: iec101.DataMsg, *,
        receive_time: datetime | None = None,
        batch: list[ProtocolSample] | None = None,
    ) -> None:
        key: Address = (message.asdu_address, message.io_address)
        data_type = type(message.data).__name__
        if message.cause is iec101.DataResCause.REQUEST:
            request = session.reads.get(key)
            if request is None or request.future.done():
                return
            # 主动读响应畸形/类型不受支持：请求必须显式失败，绝不以
            # 伪造的坏样本完成（合法数据 + Invalid 品质位仍正常解出
            # quality=BAD 的真实样本，走 decode_sample 成功路径）。
            try:
                sample = decode_sample(request.point_id, message, receive_time=receive_time)
            except (ValueError, TypeError) as exc:
                logger.error(
                    "IEC104 malformed read response for '%s' ca=%s ioa=%s "
                    "data_type=%s: %s: %s",
                    request.point_id,
                    key[0],
                    key[1],
                    data_type,
                    type(exc).__name__,
                    exc,
                )
                _set_exception(
                    request.future,
                    ProtocolError(
                        f"IEC104 malformed read response for '{request.point_id}' "
                        f"(ca={key[0]}, ioa={key[1]}, data_type={data_type}): {exc}"
                    ),
                )
                return
            _set_result(request.future, sample)
            return

        mapped = self._points_by_address.get(key)
        if mapped is None:
            logger.debug(
                "IEC104 drop update for unconfigured address ca=%s ioa=%s", key[0], key[1]
            )
            return
        # 自发/总召数据畸形：记错误日志并丢弃，绝不伪造值或 BAD 样本。
        try:
            sample = decode_sample(mapped.point_id, message, receive_time=receive_time)
        except (ValueError, TypeError) as exc:
            logger.error(
                "IEC104 malformed monitoring data for '%s' ca=%s ioa=%s "
                "data_type=%s — dropped: %s: %s",
                mapped.point_id,
                key[0],
                key[1],
                data_type,
                type(exc).__name__,
                exc,
            )
            return
        qoi = session.interrogation_qoi.get(message.asdu_address)
        if (
            qoi is not None
            and message.cause.value == qoi
            and message.asdu_address in session.interrogation_samples
        ):
            session.interrogation_samples[message.asdu_address].append(sample)
        if batch is None:
            self._listeners.offer((sample,))
        else:
            batch.append(sample)

    def _dispatch_read_negative(self, session: _Session, message: iec101.ReadMsg) -> None:
        # 主站发出的请求回响不会回到本侧；这里只处理从站否定（COT 44..47）。
        if not isinstance(message.cause, iec101.ReadResCause):
            return
        request = session.reads.get((message.asdu_address, message.io_address))
        if request is not None:
            _set_exception(request.future, IEC104ActiveReadRejectedError(message.cause))

    def _dispatch_command(
        self,
        session: _Session,
        message: iec104common.CommandMsg,
    ) -> None:
        waiter = session.commands.get((message.asdu_address, message.io_address))
        if waiter is None or waiter.done():
            return
        cause = message.cause
        key = (message.asdu_address, message.io_address)
        if cause is iec101.CommandResCause.ACTIVATION_CONFIRMATION:
            if message.is_negative_confirm:
                _set_result(waiter, False)
            else:
                # 肯定 ACT_CON 只表示设备受理，尚不是执行完成证据。
                session.command_accepted.add(key)
            return
        if cause is iec101.CommandResCause.ACTIVATION_TERMINATION:
            if message.is_negative_confirm:
                _set_result(waiter, False)
            elif key in session.command_accepted:
                _set_result(waiter, True)
            return
        if message.is_negative_confirm or getattr(cause, "value", None) in {44, 45, 46, 47}:
            _set_result(waiter, False)

    def _dispatch_interrogation(
        self,
        session: _Session,
        message: iec101.InterrogationMsg,
    ) -> None:
        if message.request != session.interrogation_qoi.get(message.asdu_address):
            return
        waiter = session.interrogations.get(message.asdu_address)
        if waiter is None or waiter.done():
            return
        cause = message.cause
        if cause is iec101.CommandResCause.ACTIVATION_CONFIRMATION:
            if message.is_negative_confirm:
                _set_exception(
                    waiter,
                    ProtocolError(
                        f"IEC104 station rejected interrogation for "
                        f"ca={message.asdu_address}"
                    ),
                )
            else:
                session.interrogation_accepted.add(message.asdu_address)
            return
        if cause is iec101.CommandResCause.ACTIVATION_TERMINATION:
            if message.is_negative_confirm:
                _set_exception(
                    waiter,
                    ProtocolError(
                        f"IEC104 interrogation negative ACT_TERM for "
                        f"ca={message.asdu_address}"
                    ),
                )
            elif message.asdu_address in session.interrogation_accepted:
                _set_result(waiter, None)
            return
        # UNKNOWN_TYPE/CAUSE/ASDU/IO 等否定 COT：明确失败。
        if message.is_negative_confirm or getattr(cause, "value", None) in {44, 45, 46, 47}:
            _set_exception(
                waiter,
                ProtocolError(
                    f"IEC104 interrogation rejected ({cause}) for "
                    f"ca={message.asdu_address}"
                ),
            )

    async def _send(self, session: _Session, messages: list[iec101.Msg]) -> None:
        """经 hat 编码器发送 ASDU；传输失败归一为 ProtocolConnectionError。"""
        if self._session is not session:
            raise ProtocolConnectionError("IEC104 session is no longer current")
        try:
            for chunk in self._encoder.encode(messages):
                await session.conn.send(chunk, False)
        except ConnectionError as exc:
            raise ProtocolConnectionError(f"IEC104 send failed: {exc}") from exc

    def _on_enabled(self, session: _Session, enabled: bool) -> None:
        if self._session is not session:
            return
        session.enabled = enabled
        if not enabled:
            # 对端关闭数据传输（或链路停使能）：结束在途应用请求。
            self._fail_session(
                session,
                ProtocolConnectionError("IEC104 data transfer disabled by peer"),
            )

    async def _on_session_end(self, session: _Session) -> None:
        if self._session is session:
            self._session = None
        self._fail_session(
            session,
            ProtocolConnectionError("IEC104 connection lost"),
        )
        with contextlib.suppress(Exception):
            await session.conn.async_close()

    def _invalidate_session(self, session: _Session, reason: str) -> None:
        """请求超时后禁止复用会话，隔离无法关联的旧响应。"""
        if self._session is not session:
            return
        self._session = None
        session.enabled = False
        self._fail_session(session, ProtocolConnectionError(reason))
        if session.task is not None:
            session.task.cancel()

    @classmethod
    def _fail_session(cls, session: _Session, exc: Exception) -> None:
        """结束会话内全部在途 Future（幂等）。命令路径把链路类异常映射为 UNKNOWN。"""
        if session.failed:
            return
        session.failed = True
        cls._reject_pending(session, exc)

    @staticmethod
    def _reject_pending(session: _Session, exc: Exception) -> None:
        """结束当前在途 Future 并清空等待表（不改变会话 failed 标记）。"""
        for request in session.reads.values():
            _set_exception(request.future, exc)
        for command_future in session.commands.values():
            _set_exception(command_future, exc)
        for interrogation_future in session.interrogations.values():
            _set_exception(interrogation_future, exc)
        session.reads.clear()
        session.commands.clear()
        session.command_accepted.clear()
        session.interrogations.clear()
        session.interrogation_qoi.clear()
        session.interrogation_accepted.clear()
        # 由等待方 finally 清理各自的部分结果；否则断链时结果提前丢失。

    # ------------------------------------------------------------------
    # 索引与校验
    # ------------------------------------------------------------------

    def _build_index(
        self,
        point_table: PointTable,
    ) -> tuple[dict[str, IEC104Point], dict[Address, IEC104Point]]:
        return build_iec104_index(
            list(point_table.points.values()),
            common_addresses=self._config.common_addresses,
            default_ca=self._config.default_ca,
        )

    def _validate_health_point(self) -> None:
        health_point = self._config.health_point
        if health_point is not None and health_point not in self._points_by_id:
            raise ConfigError(
                f"IEC104 health_point '{health_point}' is not present in point table "
                f"'{self._point_table_id}'"
            )

    def _require_point(self, point_id: str) -> IEC104Point:
        if not isinstance(point_id, str):
            raise ConfigError("protocol point must be a configured point key")
        mapped = self._points_by_id.get(point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point_id}' is not part of connection '{self._point_table_id}'"
            )
        return mapped

    def _require_session(self) -> _Session:
        session = self._session
        if session is None or not session.enabled:
            raise ProtocolError("IEC104 operation requires an OPEN, enabled connection")
        return session
