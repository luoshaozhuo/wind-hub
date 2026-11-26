"""ADS 连接、环境探测及有界重连。"""

from __future__ import annotations

import logging
import platform
import shutil
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pyads

from .config import Settings
from .runtime import _read_symbol_table_info

_LOG = logging.getLogger(__name__)
_SYSSERV_PORT = 10000


@dataclass
class StaticEnvironment:
    """不连接 PLC 即可确定的客户端环境；动态能力见 _probe_* 函数。"""

    os_name: str
    arch: str
    python_bits: int
    pyads_version: str | None
    adslib_available: bool
    adstool: str | None
    twincat_version: int
    ams_net_id: str
    ads_port: int


def _default_ads_port(settings: Settings) -> int:
    return (
        settings.PLC_AMS_PORT
        if settings.PLC_AMS_PORT is not None
        else (801 if settings.TWINCAT_VERSION == 2 else 851)
    )


def _inspect_environment(settings: Settings) -> StaticEnvironment:
    """静态环境检测：不连接 PLC、不修改系统，只读取客户端事实。"""
    host = platform.system()
    env = StaticEnvironment(
        os_name=host,
        arch=platform.machine(),
        python_bits=struct.calcsize("P") * 8,
        pyads_version=getattr(pyads, "__version__", None),
        adslib_available=getattr(pyads, "Connection", None) is not None,
        adstool=shutil.which("adstool"),
        twincat_version=settings.TWINCAT_VERSION,
        ams_net_id=settings.PLC_AMS_NET_ID,
        ads_port=_default_ads_port(settings),
    )
    _LOG.info("客户端系统：%s %s, Python %d 位", env.os_name, env.arch, env.python_bits)
    _LOG.info("pyads=%s, adstool=%s", env.pyads_version or "缺失", env.adstool or "未安装")
    _LOG.info(
        "目标：TwinCAT %s, NetID %s, ADS port %s", env.twincat_version, env.ams_net_id, env.ads_port
    )
    # 不用客户端系统推断 PLC 操作系统；远端信息只来自 ADS 动态探测。
    return env


def _probe_runtime(plc: pyads.Connection, settings: Settings) -> dict[str, Any]:
    """动态检测：需要已建立的 PLC 连接。"""
    info: dict[str, Any] = {}
    try:
        name, version = plc.read_device_info()
        info["device_name"] = name
        info["device_version"] = getattr(version, "version", version)
    except Exception as exc:
        info["device_info_error"] = f"{type(exc).__name__}: {exc}"
    try:
        info["symbol_count"], info["symbol_bytes"] = _read_symbol_table_info(plc, settings)
    except Exception as exc:
        info["symbol_info_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _probe_file_service(factory: Callable[[], Any]) -> tuple[bool, str]:
    """动态检测：ADS 文件服务是否可访问；成功建立即关闭。"""
    conn = None
    try:
        conn = factory()
        conn.read_state()
        return True, ""
    except Exception as exc:
        return False, f"ADS 文件服务不可达：{type(exc).__name__}: {exc}"
    finally:
        _safe_close(conn)


class _ConnectionManager:
    """统一管理当前实际 ADS 连接的所有权与重连。

    - 外部传入的初始连接默认归调用方所有，本管理器不在结束时关闭；
      但连接失效被替换时会先安全关闭旧连接（失效连接不存在"保留"价值）。
    - 由 reconnect 工厂创建的新连接归本管理器所有，结束时必须释放。
    - owns_initial=True（main 入口）时初始连接同样由管理器负责释放，
      保证成功、失败或异常路径下当前实际连接都被正确关闭。
    - 重连次数受 settings.RECONNECT_ATTEMPTS 限制，重连后必须 read_state 验证。
    """

    def __init__(
        self,
        plc: Any,
        reconnect: Callable[[], Any] | None = None,
        owns_initial: bool = False,
        *,
        settings: Settings,
    ) -> None:
        self.current = plc
        self._reconnect = reconnect
        self._owned = owns_initial
        self._attempts_left = settings.RECONNECT_ATTEMPTS

    def usable(self) -> bool:
        return _connection_usable(self.current)

    def recover(self) -> bool:
        """连接失效时尝试恢复；不无休止重试。"""
        if self.usable():
            return True
        _LOG.warning("ADS 连接已断开")
        if self._reconnect is None or self._attempts_left <= 0:
            return False
        self._attempts_left -= 1
        _safe_close(self.current)  # 失效连接先关闭，避免泄漏
        self.current = None
        self._owned = False
        try:
            candidate = self._reconnect()
        except Exception as exc:
            _LOG.warning("重连失败: %s", exc)
            return False
        # 工厂创建的连接归管理器所有：探活失败立即关闭，
        # 不等待调用者清理，也不把失效对象交给后续方案。
        if not _connection_usable(candidate):
            _LOG.warning("重连后的新连接探活失败，立即关闭")
            _safe_close(candidate)
            return False
        self.current = candidate
        self._owned = True
        return True

    def close(self) -> None:
        """释放归本管理器所有的连接；调用方所有的连接不触碰。"""
        if self._owned:
            _safe_close(self.current)
            self._owned = False


def _safe_close(conn: Any) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception as exc:
        _LOG.warning("关闭 ADS 连接失败: %s", exc)


def _connection_usable(plc: Any) -> bool:
    if plc is None:
        return False
    read_state = getattr(plc, "read_state", None)
    if not callable(read_state):
        return True  # 无状态接口的测试桩视为可用
    try:
        read_state()
        return True
    except Exception:
        return False


def _default_file_service_factory(settings: Settings) -> Callable[[], pyads.Connection]:
    """独立连接配置指定的 System Service，不依赖 Runtime 地址或状态。"""

    def factory() -> pyads.Connection:
        conn = pyads.Connection(settings.PLC_AMS_NET_ID, _SYSSERV_PORT, settings.PLC_IP)
        try:
            conn.open()
            conn.set_timeout(settings.ADS_TIMEOUT_MS)
        except Exception:
            _safe_close(conn)
            raise
        return conn

    return factory


def _is_local_route_error(exc: Exception) -> bool:
    """保守识别"本地路由缺失"：仅明确的端口/路由缺失信号。

    ADS error 6 = Target port not found（本地路由表无目标 NetID）。
    网络不可达、超时、拒绝访问、认证错误等均不匹配，绝不盲目改路由。
    """
    if getattr(exc, "err_code", None) == 6:
        return True
    text = str(exc).lower()
    return "target port not found" in text or "no route" in text


def _try_open_plc(port: int, settings: Settings) -> pyads.Connection:
    """按当前已有路由配置建立连接并验证 ADS 服务实际可用。

    open/set_timeout/read_state 任意一步失败都尝试关闭连接；
    清理阶段的异常不覆盖原始异常，仅记录日志。
    """
    plc = pyads.Connection(settings.PLC_AMS_NET_ID, port, settings.PLC_IP)
    try:
        plc.open()
        plc.set_timeout(settings.ADS_TIMEOUT_MS)
        plc.read_state()
    except Exception:
        try:
            plc.close()
        except Exception as cleanup_error:
            _LOG.warning("连接失败后的清理异常（已忽略）: %s", cleanup_error)
        raise
    return plc


def _open_connection(settings: Settings, env: StaticEnvironment | None = None) -> pyads.Connection:
    """先连接、后按需修复本地路由；远端路由绝不自动修改。

    流程：直接连接 → read_state 验证 → 成功立即返回（绝不调用 add_route）。
    仅当失败明确属于本地路由缺失、settings.ADD_LOCAL_ROUTE=True 且当前平台确实
    依赖本地路由（Windows TwinCAT Router；Linux AdsLib 按 IP 直连，跳过）
    时，修复一次本地路由并重试；重试失败同时报告原始与重试错误。
    """
    if settings.TWINCAT_VERSION not in (2, 3):
        raise ValueError("settings.TWINCAT_VERSION 只能为 2 或 3")
    port = _default_ads_port(settings)
    os_name = env.os_name if env is not None else platform.system()
    try:
        return _try_open_plc(port, settings)
    except Exception as first_error:
        if not _is_local_route_error(first_error):
            raise
        if os_name != "Windows":
            # Linux AdsLib 按 IP 直连，本地无需显式路由；路由类错误只能报告。
            raise RuntimeError(
                f"连接失败且疑似路由问题，但 {os_name} 下本工具不修改本地路由："
                f"{type(first_error).__name__}: {first_error}"
            ) from first_error
        if not settings.ADD_LOCAL_ROUTE:
            raise RuntimeError(
                f"连接失败，疑似本地路由缺失；settings.ADD_LOCAL_ROUTE=False，未自动修复："
                f"{type(first_error).__name__}: {first_error}"
            ) from first_error
        _LOG.warning("疑似本地路由缺失，按需添加一次本地路由")
        pyads.add_route(settings.PLC_AMS_NET_ID, settings.PLC_IP)  # 最多一次，绝不重复
        try:
            return _try_open_plc(port, settings)
        except Exception as retry_error:
            hint = (
                "本地路由修复后仍无法连接："
                f"原始错误 {type(first_error).__name__}: {first_error}；"
                f"重试错误 {type(retry_error).__name__}: {retry_error}"
            )
            if settings.ADD_REMOTE_ROUTE_IF_NEEDED and _is_local_route_error(retry_error):
                # pyads/AdsLib 无远端路由查询接口，无法证明 PLC 端路由缺失，
                # 只能提示人工确认，绝不自动修改 PLC 路由表。
                hint += (
                    "；仍疑似路由问题，可能是 PLC 端缺少指向本机的路由。"
                    "请在 TwinCAT 工程或 PLC 上人工确认后手动配置，"
                    "本工具不会自动修改 PLC 远端路由"
                )
            raise RuntimeError(hint) from retry_error
