"""IEC104 从站代理 TCP Server。

本模块拥有监听 socket 和每条主站连接对应的 IEC104SlaveSession task。start
与 stop 均幂等；所有 session 通过 handlers 共享同一份 DataSnapshot，因此
任一调度主站的总召都读取 Collector 已采集的同一组最新值。

Server 不主动访问现场设备，也不保存历史数据。stop 会停止监听、取消全部活动
session，并等待资源释放。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from wind_hub.adapter.inbound.iec104_slave.handlers import IEC104SlaveHandlers
from wind_hub.adapter.inbound.iec104_slave.session import IEC104SlaveSession
from wind_hub_core.model.health import HealthStatus

logger = logging.getLogger(__name__)


class IEC104SlaveServer:
    """监听调度主站连接的 IEC104 从站代理。

    Args:
        host: 监听地址。
        port: 监听端口；0 表示由操作系统分配临时端口。
        handlers: 共享的 IEC104 请求处理器。
        common_address: 从站公共地址。
    """

    def __init__(
        self,
        host: str,
        port: int,
        handlers: IEC104SlaveHandlers,
        common_address: int,
    ) -> None:
        self._host = host
        self._port = port
        self._handlers = handlers
        self._common_address = common_address

        self._server: asyncio.Server | None = None
        self._sessions: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """开始监听。

        重复调用幂等；已经监听时直接返回。

        Raises:
            OSError: socket 绑定或监听失败。
        """
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._on_client_connected,
            host=self._host,
            port=self._port,
        )
        logger.info(
            "IEC104 slave proxy listening on %s:%d (common_address=%d)",
            self._host,
            self._port,
            self._common_address,
        )

    async def stop(self) -> None:
        """停止监听并取消全部活动 session。

        重复调用幂等。session 取消和 socket 关闭阶段的次要异常不会阻断整体停机。
        """
        if self._server is None:
            return
        self._server.close()
        for task in list(self._sessions):
            task.cancel()
        if self._sessions:
            await asyncio.gather(*list(self._sessions), return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None
        logger.info("IEC104 slave proxy stopped")

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def session_count(self) -> int:
        """返回当前活动 session 数量。"""
        return len(self._sessions)

    @property
    def port(self) -> int:
        """返回实际监听端口；配置 port=0 时返回操作系统分配端口。"""
        if self._server is not None:
            sock = self._server.sockets[0]
            return int(sock.getsockname()[1])
        return self._port

    def health(self) -> HealthStatus:
        """返回从站代理健康状态；正在监听即视为 healthy。"""
        if self._server is None:
            return HealthStatus(healthy=False, message="not started")
        return HealthStatus(healthy=True, message=f"{self.session_count} session(s)")

    # ------------------------------------------------------------------
    # 连接处理
    # ------------------------------------------------------------------

    async def _on_client_connected(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._track_session(task)

        session = IEC104SlaveSession(reader, writer, self._handlers, self._common_address)
        try:
            await session.run()
        except Exception:
            logger.warning("IEC104 slave session terminated with error", exc_info=True)
        finally:
            if task is not None:
                self._untrack_session(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _track_session(self, task: asyncio.Task[None]) -> None:
        self._sessions.add(task)

    def _untrack_session(self, task: asyncio.Task[None]) -> None:
        self._sessions.discard(task)
