"""ADS (Automation Device Specification) protocol driver over pyads.

Implements :class:`~wind_hub.domain.port.outbound.ProtocolPort` for the ADS
protocol.  Points may be addressed by ``index_group`` + ``index_offset`` or by
PLC symbol name (see :mod:`wind_hub.adapter.outbound.protocol.ads.mapping`).

Batch reads dispatch on ``read_mode``: ``'sum'`` packs many points into a single
ADS Sum command via symbol addressing (``read_list_by_name``), while
``'sequential'`` issues one per-point ``Read`` with a concurrency limit.
Device-notification subscription (push mode) is supported through
:mod:`wind_hub.adapter.outbound.protocol.ads.subscription` when
``subscribe.enabled`` is set.

pyads exposes a *synchronous* API, so every blocking call is offloaded to a
worker thread via :func:`asyncio.to_thread` to avoid stalling the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from wind_hub.adapter.outbound.protocol.ads import router as ads_router
from wind_hub.adapter.outbound.protocol.ads.config import ADSConfig, from_device_config
from wind_hub.adapter.outbound.protocol.ads.mapping import ADSPoint, parse_point
from wind_hub.adapter.outbound.protocol.ads.subscription import ADSSubscription
from wind_hub.config.schema import DeviceConfig, PointConfig
from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.errors import ConfigError, ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue, Quality
from wind_hub.domain.port.outbound import (
    AcquisitionMode,
    HealthStatus,
    ProtocolPort,
    SubscriptionHandle,
)

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF_BASE = 1.0
_RECONNECT_BACKOFF_MULTIPLIER = 2.0


def _pyads() -> Any:
    """Return the ``pyads`` module, importing it lazily.

    pyads ships no ``py.typed`` marker, so it is untyped; importing it here
    (rather than at module top) also keeps the base package importable without
    the optional ``ads`` extra.
    """
    import pyads  # type: ignore[import-untyped]

    return pyads


def _plc_datatype(ads_name: str) -> Any:
    """Return the pyads ``PLCTYPE_*`` class for an ADS type name."""
    pyads = _pyads()
    return getattr(pyads, f"PLCTYPE_{ads_name}")


#: ADS 错误码：符号不存在（ADSERR_DEVICE_SYMBOLNOTFOUND）——只影响该点。
_ADSERR_SYMBOL_NOT_FOUND = 1808


def _is_point_level_ads_error(exc: BaseException) -> bool:
    """判定 ADS 错误是否属于「单点级」失败（可安全降级为 BAD 点）。

    仅识别 ``pyads.ADSError`` 且 ``err_code == 1808``（符号不存在）——
    其余错误（超时、句柄失效、传输错误）无法与连接级故障可靠区分，
    保持上抛，由外层走断线/重连路径（不伪造成功）。
    """
    pyads = _pyads()
    ads_error = getattr(pyads, "ADSError", None)
    return (
        ads_error is not None
        and isinstance(exc, ads_error)
        and getattr(exc, "err_code", None) == _ADSERR_SYMBOL_NOT_FOUND
    )


class ADSDriver:
    """ADS protocol driver.

    Not thread-safe; a single asyncio event loop owns each instance.  An
    internal :class:`asyncio.Lock` serialises ``read``/``write`` calls.
    """

    def __init__(self, cfg: DeviceConfig) -> None:
        self._cfg = cfg
        self._config: ADSConfig = from_device_config(cfg)
        self._host = cfg.endpoint.host
        self._lock = asyncio.Lock()

        self._points: dict[str, ADSPoint] = {}
        self._connected = False
        self._failed = False
        self._shutdown = False
        # route 自动修复：每个进程生命周期内每台设备最多一次，且只发生在首次
        # 连接成功之前——已成功连接过的设备掉线只走 reconnect，不再 add route。
        self._route_repair_attempted = False
        self._ever_connected = False

        self._reconnect_event = asyncio.Event()
        self._monitor_task: asyncio.Task[None] | None = None

        # pyads Connection, created lazily on connect (untyped on purpose so the
        # third-party type never leaks into this module's API).
        self._connection: Any = None

        # 活跃的 device-notification 订阅——每次 subscribe 调用创建一个
        # 独立实例（独立连接池 / cycle_time / 回调），按订阅句柄独立管理，
        # 互不影响。
        self._subscriptions: set[ADSSubscription] = set()

    # ------------------------------------------------------------------
    # point mapping
    # ------------------------------------------------------------------

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """Resolve a point table to :class:`ADSPoint` entries (last one wins)."""
        mapping: dict[str, ADSPoint] = {}
        for point in points:
            mapping[point.point_id] = parse_point(point)
        self._points = mapping

    # ------------------------------------------------------------------
    # ProtocolPort — connect / close
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the ADS connection, retrying with exponential backoff.

        首轮 retry budget 耗尽仍失败时：启动后台 monitor 持续重连（覆盖
        「PLC 比 wind-hub 晚启动/断电恢复」场景），随后仍向调用方抛出
        :class:`ProtocolError`——Runtime 如实记录连接失败，后台恢复并行进行。
        """
        async with self._lock:
            if self._connected:
                return
            self._shutdown = False
            last_exc = await self._connect_with_retry()
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            if last_exc is not None:
                self._reconnect_event.set()
                raise ProtocolError(
                    f"ADS: failed to connect to {self._host} "
                    f"after {self._config.reconnect_max_retries + 1} attempts: {last_exc}"
                ) from last_exc

    async def close(self) -> None:
        """Close the connection and stop the reconnect monitor."""
        async with self._lock:
            self._shutdown = True
            self._reconnect_event.set()
            if self._monitor_task is not None and not self._monitor_task.done():
                self._monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._monitor_task
            self._monitor_task = None
            for subscription in list(self._subscriptions):
                with contextlib.suppress(Exception):
                    await subscription.close()
            self._subscriptions.clear()
            self._close_connection()
            self._connected = False
            self._failed = False

    async def _connect_with_retry(self) -> Exception | None:
        """Attempt connection with exponential backoff.

        Returns:
            ``None`` on success, or the last exception once the retry budget is
            exhausted (after which ``self._failed`` is set).
        """
        backoff = _RECONNECT_BACKOFF_BASE
        last_exc: Exception | None = None
        budget = self._config.reconnect_max_retries + 1
        for attempt in range(budget):
            try:
                await self._connect_once_with_repair()
                self._connected = True
                self._ever_connected = True
                self._failed = False
                logger.info(
                    "ADS: connected to %s (net id %s)", self._host, self._config.target_net_id
                )
                return None
            except Exception as exc:  # connection failure — retry with backoff
                last_exc = exc
                if isinstance(exc, TimeoutError):
                    # 连接/读写超时是对端不可达的日常表现：明确标记为超时，
                    # 保持简洁 warning、不打堆栈（决策 2）。
                    logger.warning(
                        "ADS: connect attempt %d/%d timed out: %s", attempt + 1, budget, exc
                    )
                else:
                    logger.warning(
                        "ADS: connect attempt %d/%d failed: %s", attempt + 1, budget, exc
                    )
                if attempt < budget - 1:
                    await asyncio.sleep(backoff)
                    backoff = min(
                        backoff * _RECONNECT_BACKOFF_MULTIPLIER,
                        self._config.reconnect_backoff_max,
                    )
        self._failed = True
        return last_exc

    async def _connect_once_with_repair(self) -> None:
        """One connect attempt, with a one-shot route repair on first failure.

        流程：正常连接 → 成功即返回；失败且 route 修复可用（``route_repair``
        启用、本设备尚未修复过、且从未成功连接过）→ 执行一次
        ``add_route_to_plc`` 后再连接一次。修复或重连仍失败则异常原样上抛，
        由外层 retry/reconnect 机制接管；之后不再触发 add route。
        """
        try:
            await self._do_connect()
            return
        except Exception as first_exc:
            if (
                self._route_repair_attempted
                or self._ever_connected
                or not await ads_router.repair_route_once(self._host)
            ):
                raise first_exc
            self._route_repair_attempted = True
            logger.info("ADS: route repaired for %s — retrying connect once", self._host)
        await self._do_connect()

    async def _do_connect(self) -> None:
        """Create and open the pyads connection (blocking calls on a thread)."""
        pyads = _pyads()
        # ``None`` lets pyads auto-detect the Net ID from the IP address, as its
        # ``open()`` does.
        net_id = self._config.target_net_id or None
        connection = pyads.Connection(net_id, self._config.target_port, self._host)
        connection.set_timeout(int(self._config.timeout * 1000))
        await asyncio.to_thread(connection.open)
        if not connection.is_open:
            connection.close()
            raise ProtocolError(f"ADS: failed to open connection to {self._host}")
        self._connection = connection

    def _close_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()

    async def _monitor_loop(self) -> None:
        """Reconnect in the background after a transport failure is signalled.

        一轮 retry budget 耗尽不代表放弃（断电/PLC 晚启动是现场常态）：标记为
        degraded（``_failed``），等待 ``reconnect_backoff_max`` 后开启新一轮，
        直到连接成功或 driver shutdown。``close()`` 会 cancel 本协程，故
        ``asyncio.sleep`` 期间的停机由 CancelledError 保证。
        """
        while not self._shutdown:
            await self._reconnect_event.wait()
            self._reconnect_event.clear()
            if self._shutdown:
                return
            if self._connected:
                continue
            last_exc = await self._connect_with_retry()
            if last_exc is not None:
                logger.error(
                    "ADS: reconnect round exhausted (%s) — degraded; "
                    "next round in %.0fs",
                    last_exc,
                    self._config.reconnect_backoff_max,
                )
                await asyncio.sleep(self._config.reconnect_backoff_max)
                if not self._shutdown and not self._connected:
                    self._reconnect_event.set()

    def _signal_disconnect(self) -> None:
        """Mark the connection as dropped and request a background reconnect."""
        self._connected = False
        self._reconnect_event.set()

    # ------------------------------------------------------------------
    # ProtocolPort — read
    # ------------------------------------------------------------------

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """Read points using the configured batch strategy (Sum vs sequential)."""
        async with self._lock:
            if not self._connected:
                raise ProtocolError("ADS: cannot read — driver is not connected")
            try:
                if self._config.read_mode == "sum":
                    return await self._read_sum(points)
                return await self._read_sequential(points)
            except ProtocolError:
                raise
            except NotImplementedError:
                # Sum mode with index/offset addressing is a config mismatch, not
                # a wire failure — propagate it unchanged (no reconnect).
                raise
            except Exception as exc:
                # ADSError / transport failures must not leak the third-party
                # type through the port boundary; wrap and schedule a reconnect.
                self._signal_disconnect()
                raise ProtocolError(f"ADS read failed: {exc}") from exc

    async def _read_sum(self, points: list[PointRef]) -> list[PointValue]:
        """Read points via one (or more) ADS Sum commands using symbols.

        Points without a symbol (``index_group``/``index_offset`` addressing)
        are unsupported by the Sum command and raise ``NotImplementedError``.  A
        symbol whose sub-command fails (missing from the response) is reported
        as ``BAD``; an overall Sum failure raises :class:`ProtocolError`.
        """
        results: list[PointValue | None] = [None] * len(points)
        symbol_points: list[tuple[int, PointRef, ADSPoint]] = []
        for i, ref in enumerate(points):
            ap = self._points.get(ref.point_id)
            if ap is None:
                results[i] = self._bad_value(ref)
                continue
            if ap.symbol is None:
                raise NotImplementedError(
                    f"ADS Sum read requires symbol addressing; "
                    f"point '{ref.point_id}' has no symbol"
                )
            symbol_points.append((i, ref, ap))

        max_subs = self._config.max_subs_per_sum
        for start in range(0, len(symbol_points), max_subs):
            chunk = symbol_points[start : start + max_subs]
            symbols = [ap.symbol for _, _, ap in chunk]
            try:
                values = await asyncio.to_thread(self._connection.read_list_by_name, symbols)
            except Exception as exc:
                raise ProtocolError(f"ADS Sum read failed: {exc}") from exc
            for i, ref, ap in chunk:
                if ap.symbol not in values:
                    results[i] = self._bad_value(ref)
                else:
                    results[i] = PointValue(
                        device_id=ref.device_id,
                        point_id=ref.point_id,
                        value=values[ap.symbol],
                        quality=Quality.GOOD,
                        source="ads",
                    )
        return [r for r in results if r is not None]

    async def _read_sequential(self, points: list[PointRef]) -> list[PointValue]:
        """Read each point with an individual ``Read``, concurrency-limited.

        寻址优先级：点配置了 ``symbol`` 时用 ``read_by_name``（Symbol 寻址），
        否则回退 ``index_group``/``index_offset`` 兼容寻址。

        部分失败语义：单点级错误（ADS 1808 符号不存在）降级为该点
        ``Quality.BAD``，不影响批次内其它点；其余错误（超时/传输）无法与
        连接级故障可靠区分，继续上抛走断线/重连路径。
        """
        sem = asyncio.Semaphore(self._config.max_concurrent_reads)

        async def read_one(ref: PointRef) -> PointValue:
            ap = self._points.get(ref.point_id)
            if ap is None:
                return self._bad_value(ref)
            plctype = _plc_datatype(ap.data_type)
            try:
                async with sem:
                    if ap.symbol is not None:
                        value = await asyncio.to_thread(
                            self._connection.read_by_name, ap.symbol, plctype
                        )
                    else:
                        value = await asyncio.to_thread(
                            self._connection.read, ap.index_group, ap.index_offset, plctype
                        )
            except Exception as exc:
                if _is_point_level_ads_error(exc):
                    logger.warning(
                        "ADS: symbol not found for point '%s' — marked BAD", ref.point_id
                    )
                    return self._bad_value(ref)
                raise
            return PointValue(
                device_id=ref.device_id,
                point_id=ref.point_id,
                value=value,
                quality=Quality.GOOD,
                source="ads",
            )

        return await asyncio.gather(*(read_one(ref) for ref in points))

    @staticmethod
    def _bad_value(ref: PointRef) -> PointValue:
        """A ``BAD`` value for an unknown or failed point."""
        return PointValue(
            device_id=ref.device_id,
            point_id=ref.point_id,
            value=None,
            quality=Quality.BAD,
            source="ads",
        )

    # ------------------------------------------------------------------
    # ProtocolPort — write
    # ------------------------------------------------------------------

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """Batch-write commands; one :class:`CommandResult` per command."""
        async with self._lock:
            if not cmds:
                return []
            if not self._connected:
                raise ProtocolError("ADS: cannot write — driver is not connected")
            try:
                return await self._write_impl(cmds)
            except ProtocolError:
                raise
            except Exception as exc:
                self._signal_disconnect()
                raise ProtocolError(f"ADS write failed: {exc}") from exc

    async def _write_impl(self, cmds: list[Command]) -> list[CommandResult]:
        results: list[CommandResult] = []
        for cmd in cmds:
            ap = self._points.get(cmd.point_id)
            if ap is None:
                results.append(self._failed_result(cmd, f"unknown point '{cmd.point_id}'"))
                continue
            plctype = _plc_datatype(ap.data_type)
            if ap.symbol is not None:
                # 写入优先级：symbol 存在时永远走 Symbol 寻址，不用 index。
                await asyncio.to_thread(
                    self._connection.write_by_name, ap.symbol, cmd.value, plctype
                )
            else:
                await asyncio.to_thread(
                    self._connection.write,
                    ap.index_group,
                    ap.index_offset,
                    cmd.value,
                    plctype,
                )
            results.append(CommandResult(command_id=cmd.command_id, success=True))
        return results

    @staticmethod
    def _failed_result(cmd: Command, error: str) -> CommandResult:
        return CommandResult(command_id=cmd.command_id, success=False, error=error)

    # ------------------------------------------------------------------
    # ProtocolPort — subscribe / health
    # ------------------------------------------------------------------

    @property
    def acquisition_mode(self) -> AcquisitionMode:
        """``subscribe_enabled`` → 订阅推送；否则主动轮询（Sum read）。"""
        if self._config.subscribe_enabled:
            return AcquisitionMode.SUBSCRIBE
        return AcquisitionMode.POLL

    async def subscribe(
        self,
        points: list[PointRef],
        callback: Callable[[PointValue], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        """Subscribe to spontaneous device-notification updates.

        Requires ``subscribe_enabled`` in the device config; otherwise raises
        :class:`NotImplementedError`.  Unknown points (not in the point table)
        are skipped.

        每次调用创建一个**独立**订阅（独立连接池、独立 ``cycle_time``、
        独立回调）——同一 symbol 可被多个 TaskInstance 以不同节拍订阅，
        互不覆盖；关闭返回的句柄只注销本次订阅。

        ``interval`` 即 notification 的 ``cycle_time``（秒，来自
        Task.interval），必填且 > 0。
        """
        if not self._config.subscribe_enabled:
            raise NotImplementedError(
                "ADS subscription is not enabled — set subscribe_enabled=true "
                "in the device config"
            )
        if interval is None or interval <= 0:
            raise ConfigError(
                f"ADS subscription on device '{self._cfg.device_id}' requires "
                f"interval > 0 (used as notification cycle_time), got {interval}"
            )
        subscription = ADSSubscription(
            config=self._config,
            device_id=self._cfg.device_id,
            host=self._host,
            loop=asyncio.get_running_loop(),
            on_data=callback,
            cycle_time=interval,
        )
        ads_points = [self._points[ref.point_id] for ref in points if ref.point_id in self._points]
        try:
            await subscription.subscribe(ads_points)
        except Exception:
            await subscription.close()
            raise
        self._subscriptions.add(subscription)
        return _ADSSubscriptionHandle(self, subscription)

    def health(self) -> HealthStatus:
        """Return cached connection health."""
        if self._failed:
            return HealthStatus(healthy=False, message="degraded: reconnecting in background")
        if not self._connected:
            return HealthStatus(healthy=False, message="not connected")
        return HealthStatus(healthy=True)


class _ADSSubscriptionHandle:
    """一次 ADS 订阅的句柄——close 只注销本次订阅（实现 SubscriptionHandle）。"""

    def __init__(self, driver: ADSDriver, subscription: ADSSubscription) -> None:
        self._driver = driver
        self._subscription = subscription

    async def close(self) -> None:
        self._driver._subscriptions.discard(self._subscription)
        await self._subscription.close()


# ---------------------------------------------------------------------------
# self-registration
# ---------------------------------------------------------------------------

from wind_hub.infra.protocol_registry import register_protocol  # noqa: E402


@register_protocol("ads")
def _create_ads(cfg: DeviceConfig) -> ProtocolPort:
    return ADSDriver(cfg)
