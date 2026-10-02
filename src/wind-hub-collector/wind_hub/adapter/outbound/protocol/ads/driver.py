"""基于 pyads 的 ADS ProtocolPort 实现。

点表可配置 PLC symbol 或 index_group/index_offset。symbol-only 点不会在周期
采集时查询符号：Driver 在建立连接后解析一次 symbol 并缓存 index 地址；后续
read/write 均按 index_group/index_offset 执行。

read_mode=sum 使用地址型 ADS Sum Read，并按 max_subs_per_sum 分块；
sequential 逐点读取并受 max_concurrent_reads 限制。pyads 为同步 API，所有阻塞
调用都通过 asyncio.to_thread 离开事件循环。

Driver 由单个 asyncio event loop 持有，内部 Lock 串行化 read/write。传输级
失败触发后台重连；单点 symbol-not-found 可降级为 BAD 点，不伪装为连接成功。
pyads 未提供完整类型标注，第三方对象只在本适配边界内使用 Any。
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

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
    """延迟导入 pyads，并隔离未类型化第三方边界。

    Returns:
        pyads 模块对象；因第三方缺少完整类型标注，此处使用 Any。

    Raises:
        ImportError: 实际使用 ADS 但环境未安装 ads extra。
    """
    import pyads  # type: ignore[import-untyped]

    return pyads


def _plc_datatype(ads_name: str) -> Any:
    """返回 ADS 类型名对应的 pyads PLCTYPE；第三方 ctypes 类型以 Any 隔离。"""
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
    """单设备 ADS 协议驱动。

    Args:
        cfg: 已解析的设备配置。

    Notes:
        实例不保证线程安全；一个 asyncio event loop 独占实例，read/write 由
        内部 Lock 串行化。subscription 使用独立 pyads connection pool。
    """

    def __init__(self, cfg: DeviceConfig) -> None:
        self._cfg = cfg
        self._config: ADSConfig = from_device_config(cfg)
        self._host = cfg.endpoint.host
        self._lock = asyncio.Lock()

        self._points: dict[str, ADSPoint] = {}
        self._mapping_revision = 0
        self._resolved_revision = -1
        self._connected = False
        self._failed = False
        self._shutdown = False

        self._reconnect_event = asyncio.Event()
        self._monitor_task: asyncio.Task[None] | None = None

        # pyads Connection 在 connect 时创建；Any 仅隔离第三方未类型化对象，
        # 不进入 ProtocolPort 公开接口。
        self._connection: Any = None

        # 活跃的 device-notification 订阅——每次 subscribe 调用创建一个
        # 独立实例（独立连接池 / cycle_time / 回调），按订阅句柄独立管理，
        # 互不影响。
        self._subscriptions: set[ADSSubscription] = set()

    # ------------------------------------------------------------------
    # 点表映射
    # ------------------------------------------------------------------

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """加载点表映射。

        symbol-only 点不会在这里访问 PLC；连接建立后统一解析一次并缓存。
        热重载重新注入点表时递增 revision，下一次读之前只重新解析一次。
        """
        mapping: dict[str, ADSPoint] = {}
        for point in points:
            mapping[point.point_id] = parse_point(point)
        self._points = mapping
        self._mapping_revision += 1

    # ------------------------------------------------------------------
    # ProtocolPort：连接生命周期
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """建立 ADS 连接，并按指数退避执行首次重试。

        首轮 retry budget 用尽仍失败时启动后台 monitor 持续重连，同时向调用方
        抛出 ProtocolError，使 Runtime 如实记录初始连接失败。

        Raises:
            ProtocolError: 首轮连接预算耗尽仍未连接。
        """
        async with self._lock:
            if self._connected:
                return
            self._shutdown = False
            last_exc = await self._connect_with_retry()
            if self._monitor_task is None or self._monitor_task.done():
                self._monitor_task = asyncio.create_task(self._monitor_loop())
            if last_exc is not None:
                self._reconnect_event.set()
                raise ProtocolError(
                    f"ADS: failed to connect to {self._host} "
                    f"after {self._config.reconnect_max_retries + 1} attempts: {last_exc}"
                ) from last_exc

    async def close(self) -> None:
        """关闭 ADS 连接、后台重连任务和全部 notification 订阅。

        重复调用安全；订阅关闭异常被隔离，避免阻断其余资源释放。
        """
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
        """执行一次有限预算的指数退避连接。

        Returns:
            成功返回 None；预算耗尽返回最后一个异常，并把 driver 标记为 failed。
        """
        backoff = _RECONNECT_BACKOFF_BASE
        last_exc: Exception | None = None
        budget = self._config.reconnect_max_retries + 1
        for attempt in range(budget):
            try:
                await self._do_connect()
                self._connected = True
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

    async def _do_connect(self) -> None:
        """创建唯一 ADS Connection，并在首连时解析 symbol 地址。

        重连前先关闭旧 Connection，避免传输失败后残留的 open 对象导致
        同一 PLC 被重复建连。已解析的点表地址在 Driver 生命周期内复用。
        """
        pyads = _pyads()
        self._close_connection()
        net_id = self._config.target_net_id or None
        connection = pyads.Connection(net_id, self._config.target_port, self._host)
        connection.set_timeout(int(self._config.timeout * 1000))
        await asyncio.to_thread(connection.open)
        if not connection.is_open:
            connection.close()
            raise ProtocolError(f"ADS: failed to open connection to {self._host}")
        self._connection = connection
        await self._resolve_points_once()

    async def _resolve_points_once(self) -> None:
        """解析当前点表中尚未拥有 index 地址的 symbol，并缓存结果。

        单个 symbol 不存在只保留为未解析点，后续读时返回 BAD；连接级异常
        继续上抛，让现有 reconnect 机制处理。一个 mapping revision 至多
        执行一次解析，避免每个采样周期重复查询符号信息。
        """
        if self._resolved_revision == self._mapping_revision:
            return
        for point_id, point in list(self._points.items()):
            if point.address_resolved:
                continue
            if point.symbol is None:
                continue
            try:
                symbol = await asyncio.to_thread(self._connection.get_symbol, point.symbol)
            except Exception as exc:
                if _is_point_level_ads_error(exc):
                    logger.warning(
                        "ADS: cannot resolve symbol '%s' for point '%s'",
                        point.symbol,
                        point_id,
                    )
                    continue
                raise
            index_group = getattr(symbol, "index_group", None)
            index_offset = getattr(symbol, "index_offset", None)
            if not isinstance(index_group, int) or not isinstance(index_offset, int):
                logger.warning(
                    "ADS: symbol '%s' returned invalid address for point '%s'",
                    point.symbol,
                    point_id,
                )
                continue
            size = point.size
            plc_type = getattr(symbol, "plc_type", None)
            if plc_type is not None:
                with contextlib.suppress(TypeError):
                    size = ctypes.sizeof(plc_type)
            self._points[point_id] = replace(
                point,
                index_group=index_group,
                index_offset=index_offset,
                size=size,
                address_resolved=True,
            )
        self._resolved_revision = self._mapping_revision

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
        """标记连接已断开，并唤醒后台重连循环。"""
        self._connected = False
        self._reconnect_event.set()

    # ------------------------------------------------------------------
    # ProtocolPort：读取
    # ------------------------------------------------------------------

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """按配置的 Sum/sequential 策略批量读取点位。

        传输级异常会转换为 ProtocolError 并触发后台重连；单点可识别错误可返回
        Quality.BAD，不中断同批其他点。
        """
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
        """按已解析 index_group/index_offset 执行 ADS SUM read。

        symbol 仅在连接/点表变更时解析；周期采集不再调用 read_by_name 或
        read_list_by_name。固定长度基础类型按 max_subs_per_sum 分块批量读。
        未解析点和单个子命令错误降级为 BAD，不影响同批其他点。
        """
        await self._resolve_points_once()
        results: list[PointValue | None] = [None] * len(points)
        fixed: list[tuple[int, PointRef, ADSPoint]] = []
        variable: list[tuple[int, PointRef, ADSPoint]] = []

        for index, ref in enumerate(points):
            point = self._points.get(ref.point_id)
            if point is None or not point.address_resolved:
                results[index] = self._bad_value(ref)
                continue
            if point.size <= 0:
                variable.append((index, ref, point))
            else:
                fixed.append((index, ref, point))

        max_subs = self._config.max_subs_per_sum
        for start in range(0, len(fixed), max_subs):
            chunk = fixed[start : start + max_subs]
            addresses = [
                (point.index_group, point.index_offset, point.size)
                for _, _, point in chunk
            ]
            raw = await asyncio.to_thread(self._sum_read_bytes, addresses)
            data_offset = 4 * len(chunk)
            for item_index, (result_index, ref, point) in enumerate(chunk):
                error = struct.unpack_from("<I", raw, item_index * 4)[0]
                value_bytes = raw[data_offset : data_offset + point.size]
                data_offset += point.size
                if error:
                    logger.warning(
                        "ADS: sum read sub-command failed point='%s' error=%d",
                        ref.point_id,
                        error,
                    )
                    results[result_index] = self._bad_value(ref)
                    continue
                results[result_index] = PointValue(
                    device_id=ref.device_id,
                    point_id=ref.point_id,
                    value=self._decode_value(value_bytes, point),
                    quality=Quality.GOOD,
                    source="ads",
                )

        for result_index, ref, point in variable:
            try:
                value = await asyncio.to_thread(
                    self._connection.read,
                    point.index_group,
                    point.index_offset,
                    _plc_datatype(point.data_type),
                )
            except Exception as exc:
                if _is_point_level_ads_error(exc):
                    results[result_index] = self._bad_value(ref)
                    continue
                raise
            results[result_index] = PointValue(
                device_id=ref.device_id,
                point_id=ref.point_id,
                value=value,
                quality=Quality.GOOD,
                source="ads",
            )

        return [result for result in results if result is not None]

    def _sum_read_bytes(self, addresses: list[tuple[int, int, int]]) -> bytes:
        """调用 pyads 地址型 ADS SUM read。

        pyads Connection 没有公开的按地址列表方法，因此适配器在第三方库
        边界内调用 pyads.pyads_ex.adsSumReadBytes；高层不会接触该内部 API。
        """
        from pyads.pyads_ex import adsSumReadBytes  # type: ignore[import-untyped]

        return bytes(
            adsSumReadBytes(
                self._connection._port,
                self._connection._adr,
                addresses,
            )
        )

    @staticmethod
    def _decode_value(raw: bytes, point: ADSPoint) -> Any:
        """按已知基础 ADS 类型解码 SUM read 数据。"""
        if point.data_type == "STRING":
            return raw.split(b"\x00", 1)[0].decode("utf-8")
        plc_type = _plc_datatype(point.data_type)
        value = plc_type.from_buffer_copy(raw)
        return value.value if hasattr(value, "value") else value

    async def _read_sequential(self, points: list[PointRef]) -> list[PointValue]:
        """按已解析 index_group/index_offset 逐点读取，并限制并发。"""
        await self._resolve_points_once()
        sem = asyncio.Semaphore(self._config.max_concurrent_reads)

        async def read_one(ref: PointRef) -> PointValue:
            point = self._points.get(ref.point_id)
            if point is None or not point.address_resolved:
                return self._bad_value(ref)
            try:
                async with sem:
                    value = await asyncio.to_thread(
                        self._connection.read,
                        point.index_group,
                        point.index_offset,
                        _plc_datatype(point.data_type),
                    )
            except Exception as exc:
                if _is_point_level_ads_error(exc):
                    logger.warning(
                        "ADS: address read failed for point '%s' — marked BAD",
                        ref.point_id,
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
        """为未知点或单点读取失败构造 Quality.BAD 的 PointValue。"""
        return PointValue(
            device_id=ref.device_id,
            point_id=ref.point_id,
            value=None,
            quality=Quality.BAD,
            source="ads",
        )

    # ------------------------------------------------------------------
    # ProtocolPort：写入
    # ------------------------------------------------------------------

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """批量写入命令，并按输入顺序返回 CommandResult。

        未连接或传输失败抛 ProtocolError；未知点/未解析地址作为单命令失败返回。
        """
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
            if not ap.address_resolved:
                results.append(
                    self._failed_result(cmd, f"unresolved ADS address for point '{cmd.point_id}'")
                )
                continue
            plctype = _plc_datatype(ap.data_type)
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
    # ProtocolPort：订阅与健康状态
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
        """建立 ADS device-notification 订阅。

        Args:
            points: 需要订阅的点引用。
            callback: 每个推送点值的异步回调。
            interval: notification cycle_time，来自 Task.interval，必须大于 0。

        Returns:
            只管理本次订阅生命周期的 SubscriptionHandle。

        Raises:
            NotImplementedError: 设备未启用 subscribe_enabled。
            ConfigError: interval 未提供或不大于 0。
            Exception: pyads notification 注册失败；失败时会先关闭临时订阅资源。

        Notes:
            每次调用创建独立 connection pool 和回调，同一 symbol 可被不同
            Task Instance 以不同节拍订阅，互不覆盖。
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
        """返回缓存的连接健康状态；不执行实时网络 I/O。"""
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
# 协议自注册
# ---------------------------------------------------------------------------

from wind_hub.infra.protocol_registry import register_protocol  # noqa: E402


@register_protocol("ads")
def _create_ads(cfg: DeviceConfig) -> ProtocolPort:
    return ADSDriver(cfg)
