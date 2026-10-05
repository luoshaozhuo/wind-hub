"""IEC104 从站（slave）能力的 c104 适配。

把 c104.Server/Station/Point 包装为 wind-hub 的从站服务：监听调度主站
连接、由 lib60870-C 自动完成 STARTDT/TESTFR 与总召（GI）响应、按 IOA 维护
最新值。本模块不实现任何 TCP/APCI/ASDU 逻辑。

控制方向：从站是只读代理，不注册任何控制点。对未注册点的控制命令由
lib60870-C 按库默认语义处理（不接受应用层回调），wind-hub 不再自行构造
否定确认 ASDU。

c104.Server 的 start/stop 会创建/回收内部线程，经 executor 调用以避免
阻塞事件循环；Point 更新是线程安全的，可由 asyncio 线程直接调用。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from wind_hub_core.model.errors import ConfigError, ProtocolError
from wind_hub_core.model.health import HealthStatus

if TYPE_CHECKING:
    import c104
else:  # pragma: no cover - 依赖存在性由构造时守卫
    try:
        import c104
    except ImportError:
        c104 = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class IEC104SlaveServer:
    """监听调度主站连接的 IEC104 从站服务。

    Args:
        host: 监听地址。
        port: 监听端口。
        common_address: 从站公共地址 CASDU。

    Notes:
        c104 不允许配置 port=0 后查询实际端口；需要临时端口的调用方必须
        自行分配空闲端口再传入。构造即创建底层 Server/Station，因此
        :meth:`update_point` 可在 :meth:`start` 之前调用。
    """

    def __init__(self, host: str, port: int, common_address: int) -> None:
        if c104 is None:
            raise ConfigError(
                "IEC104 支持需要可选依赖 c104（安装 extras 'iec104' 后重试）"
            )
        server = c104.Server(ip=host, port=port)
        station = server.add_station(common_address=common_address)
        if station is None:
            raise ConfigError(
                f"IEC104: invalid station common_address={common_address}"
            )
        self._host = host
        self._port = port
        self._server = server
        self._station = station

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """开始监听；重复调用幂等。

        Raises:
            OSError: 端口绑定失败。
        """
        if self._server.is_running:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._server.start)
        except Exception as exc:
            # rebuild_sink 的热重载回滚依赖 OSError 判定绑定失败；c104 对
            # bind 失败抛 RuntimeError，这里统一归一为 OSError 保持既有契约。
            raise OSError(
                f"IEC104: bind {self._host}:{self._port} failed: {exc}"
            ) from exc
        logger.info(
            "IEC104 slave server listening on %s:%d", self._host, self._port
        )

    async def stop(self) -> None:
        """停止监听并断开全部主站连接；重复调用幂等。"""
        if not self._server.is_running:
            return
        await asyncio.get_running_loop().run_in_executor(None, self._server.stop)
        logger.info("IEC104 slave server stopped on %s:%d", self._host, self._port)

    # ------------------------------------------------------------------
    # 点值维护
    # ------------------------------------------------------------------

    def update_point(self, ioa: int, point_type: c104.Type, info: c104.Information) -> None:
        """按 IOA 创建或更新一个监视点。

        Args:
            ioa: Information Object Address。
            point_type: 监视方向 c104.Type（M_*）。
            info: 携带值、品质与源时标的 Information。

        Notes:
            同一 IOA 重复调用要求 point_type 一致；c104 Station 以 IOA 为
            唯一键，类型冲突属于配置错误。
        """
        point = self._station.get_point(ioa)
        if point is None:
            point = self._station.add_point(io_address=ioa, type=point_type)
            if point is None:
                raise ProtocolError(f"IEC104: cannot register point at IOA {ioa}")
        elif point.type != point_type:
            raise ConfigError(
                f"IEC104: IOA {ioa} already registered as {point.type.name}, "
                f"cannot update as {point_type.name}"
            )
        point.info = info

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def port(self) -> int:
        """返回配置监听端口。"""
        return self._port

    @property
    def session_count(self) -> int:
        """返回当前活动主站连接数。"""
        return self._server.active_connection_count

    def health(self) -> HealthStatus:
        """返回从站服务健康状态；正在监听即视为 healthy。"""
        if not self._server.is_running:
            return HealthStatus(healthy=False, message="not started")
        return HealthStatus(
            healthy=True, message=f"{self.session_count} session(s)"
        )
